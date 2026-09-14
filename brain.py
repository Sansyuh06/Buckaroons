#!/usr/bin/env python3
"""
brain.py — Core logic for the local video factory.

Stages:
  1. PDF ingestion  → library_index.json
  2. Topic picker    → topic.json   (Ollama qwen3:8b)
  3. Script writer   → script.json  (grounded retrieval + Ollama)
  4. TTS narration   → narration.wav (edge-tts, omnivoice fallback)
  5. MPT production  → final_long.mp4
  6. vid-clipper     → shorts / clips
"""

import asyncio
import json
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pdfplumber
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
QUANTUM_DIR = Path(r"D:\fyeshi\project\buckaroon\quantum")
OUTPUT_ROOT = Path(r"D:\fyeshi\project\buckaroon\output")
MPT_DIR = Path(r"D:\fyeshi\project\buckaroon\MoneyPrinterTurbo")
VID_CLIPPER_DIR = Path(r"D:\fyeshi\project\buckaroon\vid-clipper")

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:8b"

MPT_API_URL = "http://127.0.0.1:8080"
MPT_POLL_INTERVAL = 20  # seconds

EDGE_TTS_VOICE = "en-US-AndrewMultilingualNeural"

# ffmpeg path (winget-installed location)
FFMPEG_BIN = Path(os.environ.get("FFMPEG_BIN", ""))
_WINGET_FFMPEG = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
for _p in _WINGET_FFMPEG.glob("Gyan.FFmpeg*/ffmpeg-*/bin"):
    if (_p / "ffmpeg.exe").exists():
        FFMPEG_BIN = _p
        break

# Ensure ffmpeg is on PATH
if FFMPEG_BIN and str(FFMPEG_BIN) not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{FFMPEG_BIN}{os.pathsep}{os.environ.get('PATH', '')}"

logger = logging.getLogger("brain")


# ===========================================================================
# STAGE 1: PDF Ingestion
# ===========================================================================

def _extract_pdf_info(pdf_path: Path) -> dict:
    """Extract metadata, headings, and preview text from a single PDF."""
    info = {
        "file": str(pdf_path),
        "filename": pdf_path.name,
        "title": pdf_path.stem,
        "pages": 0,
        "headings": [],
        "chapters": [],
        "size_bytes": pdf_path.stat().st_size,
    }

    try:
        with pdfplumber.open(pdf_path) as pdf:
            info["pages"] = len(pdf.pages)

            # Try to get title from metadata
            meta = pdf.metadata or {}
            if meta.get("Title"):
                info["title"] = meta["Title"]

            # Extract headings and chapter previews
            full_text_parts = []
            current_heading = "Introduction"
            chapter_text = []

            for i, page in enumerate(pdf.pages):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""

                if not text.strip():
                    continue

                full_text_parts.append(text)

                # Detect headings: lines that are short, title-case or ALL CAPS
                for line in text.split("\n"):
                    stripped = line.strip()
                    if not stripped or len(stripped) > 120:
                        continue
                    # Heuristic: heading if short, starts with number or is title-case
                    if (len(stripped) < 80 and
                        (re.match(r"^(Chapter|CHAPTER|Section|SECTION|\d+[\.\)]\s)", stripped) or
                         (stripped.isupper() and len(stripped) > 3) or
                         re.match(r"^\d+\s+[A-Z]", stripped))):
                        if chapter_text:
                            info["chapters"].append({
                                "heading": current_heading,
                                "page_start": max(0, i - len(chapter_text)),
                                "preview": " ".join(chapter_text)[:2000],
                            })
                        current_heading = stripped
                        chapter_text = []
                        if stripped not in info["headings"]:
                            info["headings"].append(stripped)
                    else:
                        chapter_text.append(stripped)

            # Add last chapter
            if chapter_text:
                info["chapters"].append({
                    "heading": current_heading,
                    "page_start": max(0, info["pages"] - 1),
                    "preview": " ".join(chapter_text)[:2000],
                })

            # Store a general preview (first ~2000 chars)
            info["text_preview"] = " ".join(full_text_parts)[:2000]

    except Exception as e:
        logger.warning(f"Failed to parse {pdf_path.name}: {e}")
        info["error"] = str(e)

    return info


def ingest_pdfs(pdf_dir: Path = QUANTUM_DIR, output_root: Path = OUTPUT_ROOT) -> Path:
    """
    Walk pdf_dir recursively, extract metadata and previews.
    Save library_index.json to output_root.
    """
    logger.info(f"Ingesting PDFs from {pdf_dir}")
    output_root.mkdir(parents=True, exist_ok=True)

    index = {
        "created_at": datetime.now().isoformat(),
        "source_dir": str(pdf_dir),
        "pdfs": [],
    }

    pdf_files = sorted(pdf_dir.rglob("*.pdf"))
    logger.info(f"Found {len(pdf_files)} PDF files")

    for pdf_path in pdf_files:
        logger.info(f"  Processing: {pdf_path.name}")
        info = _extract_pdf_info(pdf_path)
        index["pdfs"].append(info)

    index["total_pdfs"] = len(index["pdfs"])

    index_path = output_root / "library_index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    logger.info(f"Library index saved: {index_path} ({len(index['pdfs'])} PDFs)")
    return index_path


# ===========================================================================
# STAGE 2: Topic Picker + Script Writer (Ollama)
# ===========================================================================

def _ollama_generate(prompt: str, system: str = "", seed: int = 42,
                     temperature: float = 0.7) -> str:
    """Call Ollama generate API. Returns the response text."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "seed": seed,
            "temperature": temperature,
            "num_predict": 4096,
        },
    }
    if system:
        payload["system"] = system

    logger.info(f"Calling Ollama ({OLLAMA_MODEL})...")
    resp = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=300)
    resp.raise_for_status()
    result = resp.json()
    return result.get("response", "")


def _extract_json_from_response(text: str) -> dict:
    """Extract JSON object from LLM response that may contain markdown fences or thinking tags."""
    # Strip thinking tags from reasoning models (qwen3, deepseek-r1)
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()

    # Try to find JSON in code blocks first
    json_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", text)
    if json_match:
        code_block = json_match.group(1).strip()
        try:
            return json.loads(code_block)
        except json.JSONDecodeError:
            pass

    # Try to find a JSON object by matching outermost braces
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract JSON from LLM response:\n{text[:500]}")


def pick_topic(library_index_path: Path, task_dir: Path,
               seed: int = 42) -> dict:
    """
    Use Ollama to pick ONE PDF and ONE concept for a video.
    Returns topic dict and saves to task_dir/topic.json.
    """
    logger.info("Picking topic via Ollama...")

    with open(library_index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    # Build a compact summary for the LLM
    summary_lines = []
    for i, pdf in enumerate(index["pdfs"]):
        headings_str = ", ".join(pdf.get("headings", [])[:10])
        preview = pdf.get("text_preview", "")[:500]
        summary_lines.append(
            f"[PDF {i}] {pdf['filename']} ({pdf['pages']} pages)\n"
            f"  Title: {pdf['title']}\n"
            f"  Headings: {headings_str}\n"
            f"  Preview: {preview}\n"
        )

    catalog = "\n".join(summary_lines)

    system = (
        "You are a science-video topic picker. You pick surprising, counterintuitive "
        "concepts from quantum computing textbooks that would make engaging 60-180 second "
        "educational videos for a general audience. Focus on misconceptions people have."
    )

    prompt = f"""Here is a library of {len(index['pdfs'])} quantum-computing PDFs:

{catalog}

Pick ONE pdf and ONE specific concept that would make a fascinating educational short video.
Choose something where there's a common misconception that can be debunked.

Respond with ONLY a JSON object (no other text):
{{
  "pdf_index": <int>,
  "pdf_filename": "<filename>",
  "topic": "<specific concept>",
  "angle": "<the misconception to debunk and why it's wrong>",
  "target_seconds": <60-180>
}}

/no_think"""

    response = _ollama_generate(prompt, system=system, seed=seed, temperature=0.7)
    topic = _extract_json_from_response(response)

    # Validate and enrich
    pdf_idx = topic.get("pdf_index", 0)
    if pdf_idx < 0 or pdf_idx >= len(index["pdfs"]):
        pdf_idx = 0
    topic["pdf_file"] = index["pdfs"][pdf_idx]["file"]
    topic["pdf_filename"] = index["pdfs"][pdf_idx]["filename"]
    topic["seed"] = seed

    topic_path = task_dir / "topic.json"
    with open(topic_path, "w", encoding="utf-8") as f:
        json.dump(topic, f, indent=2, ensure_ascii=False)

    logger.info(f"Topic selected: {topic.get('topic', 'unknown')} from {topic['pdf_filename']}")
    return topic


def _retrieve_excerpts(pdf_path: Path, keywords: list[str],
                       max_excerpts: int = 5) -> list[dict]:
    """Search a PDF for pages matching keywords. Return relevant excerpts."""
    excerpts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    continue
                if not text.strip():
                    continue

                text_lower = text.lower()
                score = sum(1 for kw in keywords if kw.lower() in text_lower)
                if score > 0:
                    excerpts.append({
                        "page": i + 1,
                        "score": score,
                        "text": text[:3000],
                    })

        # Sort by relevance score, take top N
        excerpts.sort(key=lambda x: x["score"], reverse=True)
        return excerpts[:max_excerpts]

    except Exception as e:
        logger.warning(f"Failed to retrieve excerpts: {e}")
        return []


def write_script(topic: dict, library_index_path: Path,
                 task_dir: Path, seed: int = 42) -> dict:
    """
    Use grounded retrieval + Ollama to write a narration script.
    Every factual claim must be traceable to an excerpt.
    """
    logger.info("Writing grounded script via Ollama...")

    pdf_path = Path(topic["pdf_file"])
    topic_text = topic.get("topic", "")
    angle = topic.get("angle", "")
    target_secs = topic.get("target_seconds", 90)

    # Extract keywords from topic and angle
    keywords = re.findall(r"\b[a-zA-Z]{4,}\b", f"{topic_text} {angle}")
    # Deduplicate and take top keywords
    seen = set()
    unique_keywords = []
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower not in seen and kw_lower not in {"that", "this", "with", "from", "have", "been", "would", "could"}:
            seen.add(kw_lower)
            unique_keywords.append(kw)
    keywords = unique_keywords[:15]

    logger.info(f"Searching PDF for keywords: {keywords}")
    excerpts = _retrieve_excerpts(pdf_path, keywords)

    if not excerpts:
        # Fallback: use first few pages
        logger.warning("No keyword matches found, using first pages as context")
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for i in range(min(5, len(pdf.pages))):
                    text = pdf.pages[i].extract_text() or ""
                    if text.strip():
                        excerpts.append({"page": i + 1, "score": 0, "text": text[:3000]})
        except Exception:
            pass

    # Build excerpt context for the LLM
    excerpt_text = ""
    for ex in excerpts:
        excerpt_text += f"\n--- Page {ex['page']} (relevance: {ex['score']}) ---\n{ex['text']}\n"

    system = (
        "You are an expert science script writer. You write narration scripts for "
        "short educational videos (60-180 seconds). Your scripts follow the format: "
        "misconception → why it's wrong → the real mechanism. "
        "CRITICAL: Every factual claim MUST come from the provided excerpts. "
        "Do NOT invent or hallucinate facts. Cite page numbers."
    )

    # Target ~150 words per minute of video
    target_words = int((target_secs / 60) * 150)

    prompt = f"""Topic: {topic_text}
Angle: {angle}
Target: ~{target_words} words ({target_secs} seconds of narration)
Source PDF: {pdf_path.name}

Here are the relevant excerpts from the source PDF:
{excerpt_text}

Write a narration script following this structure:
1. HOOK: Start with the common misconception (grab attention)
2. DEBUNK: Explain why this misconception is wrong
3. TRUTH: Explain the real mechanism, using specific details from the excerpts
4. TAKEAWAY: End with a memorable insight

RULES:
- Write naturally, as if speaking to camera
- Every factual claim must come from the excerpts above
- Target approximately {target_words} words
- Use simple language — explain like the viewer is smart but not a physicist

Respond with ONLY a JSON object:
{{
  "script": "<the full narration script text>",
  "sources": [
    {{"pdf": "<filename>", "page": <page_number>, "quote": "<brief relevant quote>"}}
  ],
  "keywords": ["<5-8 search keywords for finding relevant video footage>"],
  "aspect": "9:16",
  "estimated_seconds": <int>
}}

/no_think"""

    response = _ollama_generate(prompt, system=system, seed=seed, temperature=0.5)
    script_data = _extract_json_from_response(response)

    # Ensure required fields
    script_data.setdefault("aspect", "9:16")
    script_data.setdefault("keywords", keywords[:8])
    script_data.setdefault("sources", [])

    # Add PDF reference to all sources
    for src in script_data["sources"]:
        src.setdefault("pdf", pdf_path.name)

    script_path = task_dir / "script.json"
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump(script_data, f, indent=2, ensure_ascii=False)

    word_count = len(script_data.get("script", "").split())
    logger.info(f"Script written: {word_count} words, {len(script_data['sources'])} sources")
    return script_data


# ===========================================================================
# STAGE 3: TTS Narration (edge-tts, with omnivoice fallback)
# ===========================================================================

async def _edge_tts_generate(text: str, output_path: Path, voice: str = EDGE_TTS_VOICE):
    """Generate speech using edge-tts."""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


def generate_narration(script_text: str, task_dir: Path,
                       voice: str = EDGE_TTS_VOICE) -> Path:
    """
    Generate narration audio from script text.
    Primary: edge-tts  |  Future: omnivoice.cpp
    """
    logger.info("Generating narration audio...")
    output_path = task_dir / "narration.wav"
    mp3_path = task_dir / "narration.mp3"

    # Try omnivoice.cpp first (if running)
    try:
        resp = requests.post(
            "http://localhost:8880/v1/audio/speech",
            json={"input": script_text, "voice": "narrator", "model": "omnivoice"},
            timeout=10,
        )
        if resp.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(resp.content)
            logger.info("Narration generated via omnivoice.cpp")
            return output_path
    except Exception:
        logger.info("omnivoice.cpp not available, using edge-tts fallback")

    # Fallback: edge-tts
    try:
        asyncio.run(_edge_tts_generate(script_text, mp3_path, voice))

        # Convert MP3 to WAV for broader compatibility
        cmd = [
            "ffmpeg", "-y", "-i", str(mp3_path),
            "-ar", "44100", "-ac", "1",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        mp3_path.unlink(missing_ok=True)

        logger.info(f"Narration generated via edge-tts: {output_path}")
        return output_path

    except Exception as e:
        # If WAV conversion fails, just use the MP3
        if mp3_path.exists():
            logger.warning(f"WAV conversion failed ({e}), using MP3 directly")
            shutil.move(str(mp3_path), str(output_path.with_suffix(".mp3")))
            return output_path.with_suffix(".mp3")
        raise RuntimeError(f"TTS generation failed: {e}")


# ===========================================================================
# STAGE 4: MoneyPrinterTurbo Video Production
# ===========================================================================

def _start_mpt_server() -> subprocess.Popen:
    """Start MPT API server as a subprocess."""
    logger.info("Starting MoneyPrinterTurbo API server...")

    # Ensure config.toml exists
    config_path = MPT_DIR / "config.toml"
    example_path = MPT_DIR / "config.example.toml"
    if not config_path.exists() and example_path.exists():
        shutil.copy2(example_path, config_path)
        logger.info("Created config.toml from example")

    # Start the server writing to log file to avoid pipe buffer deadlocks
    mpt_log = OUTPUT_ROOT / "logs" / "mpt_server.log"
    mpt_log.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(mpt_log, "w", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=str(MPT_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    # Wait for server to be ready
    for i in range(60):  # Wait up to 60 seconds
        time.sleep(1)
        try:
            resp = requests.get(f"{MPT_API_URL}/docs", timeout=2)
            if resp.status_code == 200:
                logger.info("MPT server is ready")
                return proc
        except requests.ConnectionError:
            continue

    logger.error("MPT server failed to start within 60s")
    proc.terminate()
    raise RuntimeError("MoneyPrinterTurbo server failed to start")


def _check_mpt_running() -> bool:
    """Check if MPT API server is already running."""
    try:
        resp = requests.get(f"{MPT_API_URL}/docs", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def produce_video(script_data: dict, narration_path: Path, topic: dict,
                  task_dir: Path) -> Path:
    """
    Submit a video generation job to MoneyPrinterTurbo and wait for completion.
    Returns path to the final video.
    """
    logger.info("Producing video via MoneyPrinterTurbo...")

    mpt_proc = None
    if not _check_mpt_running():
        mpt_proc = _start_mpt_server()

    try:
        # Build the request
        script_text = script_data.get("script", "")
        keywords = script_data.get("keywords", [])
        topic_name = topic.get("topic", "quantum computing")

        # Determine video source: Pexels if key exists, otherwise local materials
        mpt_config = MPT_DIR / "config.toml"
        has_pexels = False
        if mpt_config.exists():
            try:
                import toml
                cfg = toml.load(mpt_config)
                pexels_keys = cfg.get("app", {}).get("pexels_api_keys", [])
                if pexels_keys and (isinstance(pexels_keys, str) or (isinstance(pexels_keys, list) and len(pexels_keys) > 0 and pexels_keys[0])):
                    has_pexels = True
            except Exception:
                pass

        if has_pexels:
            video_source = "pexels"
            video_materials = None
        else:
            video_source = "local"
            local_dir = MPT_DIR / "storage" / "local_videos"
            local_files = sorted(list(local_dir.glob("*.mp4")))
            if not local_files:
                setup_script = Path(__file__).parent / "setup_local_materials.py"
                if setup_script.exists():
                    subprocess.run([sys.executable, str(setup_script)], check=False)
                local_files = sorted(list(local_dir.glob("*.mp4")))
            video_materials = [
                {"provider": "local", "url": f.name, "duration": 15}
                for f in local_files
            ]
            logger.info(f"Using {len(video_materials)} local video background materials")

        payload = {
            "video_subject": topic_name,
            "video_script": script_text,
            "video_terms": keywords,
            "video_aspect": "9:16",
            "subtitle_enabled": True,
            "bgm_type": "random",
            "bgm_volume": 0.15,
            "video_source": video_source,
            "video_count": 1,
            "video_clip_duration": 5,
            "voice_name": EDGE_TTS_VOICE,
            "voice_volume": 1.0,
            "voice_rate": 1.0,
        }
        if video_materials:
            payload["video_materials"] = video_materials

        logger.info(f"Submitting video job: {topic_name}")
        resp = requests.post(
            f"{MPT_API_URL}/api/v1/videos",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()

        if result.get("status") != 200:
            raise RuntimeError(f"MPT rejected job: {result.get('message')}")

        task_id = result["data"]["task_id"]
        logger.info(f"MPT task created: {task_id}")

        # Poll for completion
        final_video = _poll_mpt_task(task_id, task_dir)
        return final_video

    finally:
        if mpt_proc:
            logger.info("Stopping MPT server")
            mpt_proc.terminate()
            mpt_proc.wait(timeout=10)


def _poll_mpt_task(task_id: str, task_dir: Path) -> Path:
    """Poll MPT task until complete. Returns path to final video."""
    max_polls = 180  # 60 minutes max
    for i in range(max_polls):
        time.sleep(MPT_POLL_INTERVAL)

        try:
            resp = requests.get(
                f"{MPT_API_URL}/api/v1/tasks/{task_id}",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})

            state = data.get("state", 0)
            progress = data.get("progress", 0)
            logger.info(f"  MPT task {task_id}: state={state}, progress={progress}%")

            if state == 1:  # COMPLETE
                videos = data.get("videos", []) or data.get("combined_videos", [])
                if not videos:
                    raise RuntimeError("MPT task completed but no videos returned")

                # Download the first video
                video_url = videos[0]
                logger.info(f"  Downloading: {video_url}")

                # The URL might be relative or absolute
                if video_url.startswith("http"):
                    download_url = video_url
                else:
                    download_url = f"{MPT_API_URL}/{video_url.lstrip('/')}"

                video_resp = requests.get(download_url, timeout=120)
                video_resp.raise_for_status()

                final_path = task_dir / "final_long.mp4"
                with open(final_path, "wb") as f:
                    f.write(video_resp.content)

                logger.info(f"  Video saved: {final_path}")
                return final_path

            elif state == -1:  # FAILED
                error = data.get("error", "Unknown error")
                failed_stage = data.get("failed_stage", "unknown")
                raise RuntimeError(
                    f"MPT task failed at stage '{failed_stage}': {error}"
                )

        except requests.RequestException as e:
            logger.warning(f"  Poll error (will retry): {e}")
            continue

    raise RuntimeError(f"MPT task {task_id} timed out after {max_polls * MPT_POLL_INTERVAL}s")


# ===========================================================================
# STAGE 5: vid-clipper Shorts
# ===========================================================================

def _parse_timestamp(val) -> float:
    """Safely parse timestamps that might be float, int, or MM:SS strings."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip().strip("\"'")
        if ":" in val:
            parts = val.split(":")
            try:
                if len(parts) == 2:
                    return float(parts[0]) * 60.0 + float(parts[1])
                elif len(parts) == 3:
                    return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
            except ValueError:
                pass
        try:
            return float(val)
        except ValueError:
            return 0.0
    return 0.0


def _generate_clip_recommendations(analysis_request_path: Path,
                                   output_path: Path,
                                   video_duration: float) -> None:
    """
    Read the analysis_request.md generated by vid-clipper,
    use Ollama to produce clip_recommendations.json.
    """
    logger.info("Generating clip recommendations via Ollama...")

    with open(analysis_request_path, "r", encoding="utf-8") as f:
        analysis_prompt = f.read()

    system = (
        "You are an expert viral video editor. You analyze video transcripts and "
        "identify the most engaging segments for short-form social media clips. "
        "Provide ONLY the JSON object. All start_time and end_time values must be numeric seconds."
    )

    prompt = f"""{analysis_prompt}

IMPORTANT: Respond with ONLY the JSON object, no other text.
Make sure all timestamps are numeric seconds (e.g. 0.0, 25.0) within the video duration of {video_duration} seconds.
Each clip should be 15-60 seconds long.

/no_think"""

    response = _ollama_generate(prompt, system=system, seed=42, temperature=0.3)
    recommendations = _extract_json_from_response(response)

    # Validate and fix timestamps
    clips = recommendations.get("clips", [])
    valid_clips = []
    for i, clip in enumerate(clips):
        clip.setdefault("clip_number", i + 1)
        start = _parse_timestamp(clip.get("start_time", 0))
        end = _parse_timestamp(clip.get("end_time", min(30, video_duration)))

        # Clamp to video duration
        start = max(0.0, min(start, max(0.0, video_duration - 15.0)))
        end = min(end, video_duration)
        if end <= start:
            end = min(start + 25.0, video_duration)
        if end <= start:
            start = max(0.0, end - 25.0)

        clip["start_time"] = round(start, 2)
        clip["end_time"] = round(end, 2)
        clip["duration"] = round(end - start, 2)
        clip.setdefault("title", f"Clip {i + 1}")
        clip.setdefault("description", "Engaging quantum educational segment")
        clip.setdefault("virality_score", 8)
        clip.setdefault("virality_factors", ["educational", "curiosity"])
        clip.setdefault("suggested_caption", f"Did you know this about quantum physics? #{i+1}")
        clip.setdefault("content_type", "value_bomb")
        valid_clips.append(clip)

    recommendations["clips"] = valid_clips
    recommendations.setdefault("video_summary", "Quantum computing educational video")
    recommendations.setdefault("overall_theme", "Science Education")
    recommendations.setdefault("hashtag_suggestions", [
        "#quantum", "#science", "#physics", "#education", "#shorts"
    ])

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(recommendations, f, indent=2)

    logger.info(f"Generated {len(valid_clips)} clip recommendations")


def generate_shorts(final_video_path: Path, task_dir: Path) -> list[Path]:
    """
    Run vid-clipper on the final video, handling the interactive pause.
    Returns list of generated clip paths.
    """
    logger.info("Generating shorts via vid-clipper...")

    # Get video duration
    duration = _get_video_duration(final_video_path)
    if duration <= 0:
        duration = 102.5  # fallback estimate

    # Prepare environment with UTF-8 and ffmpeg
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if FFMPEG_BIN:
        env["PATH"] = f"{FFMPEG_BIN}{os.pathsep}{env.get('PATH', '')}"

    vc_log_path = OUTPUT_ROOT / "logs" / "vid_clipper.log"
    vc_log_path.parent.mkdir(parents=True, exist_ok=True)
    vc_log_file = open(vc_log_path, "w", encoding="utf-8")

    cmd = [
        sys.executable,
        str(VID_CLIPPER_DIR / "ai_clip_generator.py"),
        str(final_video_path),
        "--skip-download",
        "--skip-transcription",
    ]

    logger.info(f"Running: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(VID_CLIPPER_DIR),
        stdin=subprocess.PIPE,
        stdout=vc_log_file,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    # Find the downloads directory that vid-clipper creates
    downloads_dir = VID_CLIPPER_DIR / "downloads"

    # Wait for analysis_request.md to appear
    analysis_found = False
    analysis_dir = None
    for wait_step in range(300):  # up to 5 minutes
        time.sleep(1)

        # Check if process has ended prematurely
        if proc.poll() is not None:
            vc_log_file.flush()
            try:
                with open(vc_log_path, "r", encoding="utf-8", errors="replace") as lf:
                    log_tail = lf.read()[-1000:]
            except Exception:
                log_tail = ""
            logger.warning(f"vid-clipper exited early (code {proc.returncode})")
            logger.warning(f"Output tail: {log_tail}")
            break

        # Look for analysis_request.md in subdirectories (most recent first)
        if downloads_dir.exists():
            subdirs = sorted(
                [d for d in downloads_dir.iterdir() if d.is_dir()],
                key=lambda d: d.stat().st_mtime,
                reverse=True,
            )
            for subdir in subdirs:
                ar_path = subdir / "analysis_request.md"
                cr_path = subdir / "clip_recommendations.json"
                if ar_path.exists() and not cr_path.exists():
                    analysis_dir = subdir
                    analysis_found = True
                    break

        if analysis_found:
            break

    if not analysis_found or analysis_dir is None:
        logger.warning("vid-clipper did not create analysis_request.md in time")
        if proc.poll() is None:
            proc.terminate()
        vc_log_file.close()
        return []

    logger.info(f"Found analysis directory: {analysis_dir}")

    # Generate clip recommendations
    ar_path = analysis_dir / "analysis_request.md"
    cr_path = analysis_dir / "clip_recommendations.json"

    _generate_clip_recommendations(ar_path, cr_path, duration)

    # Send Enter to resume vid-clipper
    try:
        proc.stdin.write("\n")
        proc.stdin.flush()
        logger.info("Sent Enter key to vid-clipper to resume clipping")
    except Exception as e:
        logger.warning(f"Failed to send Enter to vid-clipper: {e}")

    # Wait for vid-clipper to finish
    try:
        proc.wait(timeout=600)  # 10 minutes
    except subprocess.TimeoutExpired:
        logger.warning("vid-clipper timed out, terminating")
        proc.terminate()

    vc_log_file.close()

    # Collect generated clips
    clips_dir = analysis_dir / "clips"
    generated_clips = []
    if clips_dir.exists():
        insta_clips = sorted(clips_dir.glob("*_instagram.mp4"))
        target_clips = insta_clips if insta_clips else sorted(clips_dir.glob("*.mp4"))
        for clip_file in target_clips:
            dest = task_dir / "clips" / clip_file.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(clip_file, dest)
            generated_clips.append(dest)

    # Copy SUMMARY.md if it exists
    summary_src = analysis_dir / "SUMMARY.md"
    if summary_src.exists():
        shutil.copy2(summary_src, task_dir / "CLIPS_SUMMARY.md")

    logger.info(f"Generated {len(generated_clips)} short clips")
    return generated_clips


def _get_video_duration(video_path: Path) -> float:
    """Get video duration in seconds."""
    try:
        from moviepy import VideoFileClip
        with VideoFileClip(str(video_path)) as clip:
            return float(clip.duration)
    except Exception:
        pass

    try:
        cmd = ["ffmpeg", "-i", str(video_path)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr)
        if m:
            h, mins, s = m.groups()
            return int(h) * 3600 + int(mins) * 60 + float(s)
    except Exception:
        pass

    return 102.5


# ===========================================================================
# CLI Entry Points for Testing
# ===========================================================================

def setup_logging(log_dir: Optional[Path] = None):
    """Configure logging to console and optionally to file."""
    handlers = [logging.StreamHandler()]
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"brain_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Brain — Video Factory Core Logic")
    parser.add_argument("--test-ingest", action="store_true", help="Test PDF ingestion")
    parser.add_argument("--test-topic", action="store_true", help="Test topic picking")
    parser.add_argument("--test-script", action="store_true", help="Test script writing")
    parser.add_argument("--test-tts", action="store_true", help="Test TTS narration")
    parser.add_argument("--test-video", action="store_true", help="Test MPT video production")
    parser.add_argument("--test-shorts", action="store_true", help="Test vid-clipper shorts generation")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    log_dir = OUTPUT_ROOT / "logs"
    setup_logging(log_dir)

    if args.test_ingest:
        idx = ingest_pdfs()
        print(f"[OK] Library index created: {idx}")

    elif args.test_topic:
        idx_path = OUTPUT_ROOT / "library_index.json"
        if not idx_path.exists():
            idx_path = ingest_pdfs()
        task_dir = OUTPUT_ROOT / "test_topic"
        task_dir.mkdir(parents=True, exist_ok=True)
        topic = pick_topic(idx_path, task_dir, seed=args.seed)
        print(f"[OK] Topic: {json.dumps(topic, indent=2)}")

    elif args.test_script:
        idx_path = OUTPUT_ROOT / "library_index.json"
        if not idx_path.exists():
            idx_path = ingest_pdfs()
        task_dir = OUTPUT_ROOT / "test_script"
        task_dir.mkdir(parents=True, exist_ok=True)
        topic = pick_topic(idx_path, task_dir, seed=args.seed)
        script = write_script(topic, idx_path, task_dir, seed=args.seed)
        print(f"[OK] Script: {len(script.get('script', '').split())} words")
        print(f"   Sources: {len(script.get('sources', []))}")

    elif args.test_tts:
        task_dir = OUTPUT_ROOT / "test_tts"
        task_dir.mkdir(parents=True, exist_ok=True)
        path = generate_narration(
            "Quantum computing is not about being in two places at once. "
            "Let me explain what superposition really means.",
            task_dir,
        )
        print(f"[OK] Narration generated: {path}")

    elif args.test_video:
        task_dir = OUTPUT_ROOT / "test_video"
        task_dir.mkdir(parents=True, exist_ok=True)
        test_script_dir = OUTPUT_ROOT / "test_script"
        topic_path = test_script_dir / "topic.json"
        script_path = test_script_dir / "script.json"
        if not topic_path.exists() or not script_path.exists():
            idx_path = OUTPUT_ROOT / "library_index.json"
            if not idx_path.exists():
                idx_path = ingest_pdfs()
            topic = pick_topic(idx_path, task_dir, seed=args.seed)
            script_data = write_script(topic, idx_path, task_dir, seed=args.seed)
        else:
            with open(topic_path, "r", encoding="utf-8") as f:
                topic = json.load(f)
            with open(script_path, "r", encoding="utf-8") as f:
                script_data = json.load(f)

        narration_path = task_dir / "narration.wav"
        if not narration_path.exists():
            narration_path = generate_narration(script_data["script"], task_dir)

        video_path = produce_video(script_data, narration_path, topic, task_dir)
        print(f"[OK] Video generated: {video_path}")

    elif args.test_shorts:
        task_dir = OUTPUT_ROOT / "test_shorts"
        task_dir.mkdir(parents=True, exist_ok=True)
        video_path = OUTPUT_ROOT / "test_video" / "final_long.mp4"
        if not video_path.exists():
            print("[ERR] final_long.mp4 does not exist in test_video")
            sys.exit(1)
        clips = generate_shorts(video_path, task_dir)
        print(f"[OK] Generated {len(clips)} shorts: {[c.name for c in clips]}")

    else:
        parser.print_help()
