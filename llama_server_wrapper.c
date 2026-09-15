#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

int main(int argc, char* argv[]) {
    char exePath[MAX_PATH];
    if (!GetModuleFileNameA(NULL, exePath, MAX_PATH)) {
        return 1;
    }
    char* lastSlash = strrchr(exePath, '\\');
    if (lastSlash) {
        *(lastSlash + 1) = '\0';
    }
    char targetExe[MAX_PATH];
    snprintf(targetExe, sizeof(targetExe), "%sllama-server-runner.exe", exePath);

    char* fullCmd = GetCommandLineA();
    char* args = fullCmd;
    if (*args == '\"') {
        args++;
        while (*args && *args != '\"') args++;
        if (*args == '\"') args++;
    } else {
        while (*args && *args != ' ' && *args != '\t') args++;
    }
    while (*args == ' ' || *args == '\t') args++;

    size_t newCmdLen = strlen(targetExe) + 4 + strlen(args) + 1;
    char* newCmd = (char*)malloc(newCmdLen);
    if (!newCmd) return 1;
    snprintf(newCmd, newCmdLen, "\"%s\" %s", targetExe, args);

    HANDLE hJob = CreateJobObjectA(NULL, NULL);
    if (hJob) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION jeli = { 0 };
        jeli.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(hJob, JobObjectExtendedLimitInformation, &jeli, sizeof(jeli));
    }

    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags |= STARTF_USESTDHANDLES;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    ZeroMemory(&pi, sizeof(pi));

    DWORD creationFlags = CREATE_SUSPENDED;
    if (!CreateProcessA(
            targetExe,
            newCmd,
            NULL,
            NULL,
            TRUE,
            creationFlags,
            NULL,
            NULL,
            &si,
            &pi)) {
        free(newCmd);
        return (int)GetLastError();
    }

    if (hJob) {
        AssignProcessToJobObject(hJob, pi.hProcess);
    }
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);

    WaitForSingleObject(pi.hProcess, INFINITE);

    DWORD exitCode = 0;
    GetExitCodeProcess(pi.hProcess, &exitCode);
    CloseHandle(pi.hProcess);
    if (hJob) {
        CloseHandle(hJob);
    }
    free(newCmd);

    return (int)exitCode;
}
