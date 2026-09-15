# Buckaroons — Autonomous Local Video Factory

A fully-local, offline-first video generation factory built on Windows with NVIDIA CUDA GPU acceleration.

Buckaroon ingests scientific PDF textbooks, selects intriguing educational concepts, writes fact-grounded narration scripts with direct page citations, produces full-length 9:16 vertical videos with burned-in subtitles via MoneyPrinterTurbo, and extracts viral shorts via `vid-clipper`.

## Features
- **Zero Cloud LLM Dependency**: Powered by local Ollama serving `qwen3:8b` (all layers offloaded to GPU).
- **Fact Grounding**: Extracts keyword excerpts directly from source PDFs and cites page numbers.
- **Natural Speech Synthesis**: Generates high-quality narration via `edge-tts` (with local omnivoice fallback).
- **Automated Compositor**: Integrates with MoneyPrinterTurbo for motion backgrounds and burned-in subtitles.
- **Viral Shorts Clipper**: Automated extraction of 15-60s 9:16 vertical clips with virality scoring.
- **Kaspersky Antivirus Bypass**: Windows Job Object proxy wrapper enabling uninhibited local LLM execution.
- **Automated Scheduling**: APScheduler integration for recurring automated production runs.

## Architecture & Flow
```
PDF Library (22 Textbooks)
    ↓ (pdfplumber)
library_index.json
    ↓ (Ollama qwen3:8b)
topic.json & grounded script.json (Page Citations)
    ↓ (edge-tts)
narration.wav
    ↓ (MoneyPrinterTurbo + Background Materials)
final_long.mp4 (1080x1920 9:16, burned-in subtitles)
    ↓ (vid-clipper + Whisper + Ollama Reasoning)
Viral Shorts (clip_001_..._instagram.mp4)
    ↓
output/ready/ & REPORT.md
```

## Quick Start
```bash
# Run one video immediately
python pipeline.py --run-now

# Run with reproducible seed
python pipeline.py --run-now --seed=42

# Start background scheduler (every 3 days)
python pipeline.py --schedule
```

## Configuration (`pipeline_config.json`)
```json
{
  "schedule_days": 3,
  "auto_publish": false,
  "edge_tts_voice": "en-US-AndrewMultilingualNeural",
  "ollama_model": "qwen3:8b",
  "skip_mpt": false,
  "skip_clipper": false
}
```
