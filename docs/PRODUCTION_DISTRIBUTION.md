# Production distribution

## Layout

PyInstaller **one-folder** build:

```
dist/UTAnalyzer/
├── UTAnalyzer.exe
├── default-config/versions.json
├── licenses/
└── ... (Python runtime + dependencies)
```

## Build

```powershell
.\scripts\build_release.ps1
```

Optional flags: `-SkipTests`, `-SkipInstaller`.

## End-user configuration

Users configure the corporate LLM API in **Settings**:

- API base URL (OpenAI-compatible gateway)
- API key
- Model name

Network access to the internal API (VPN if required) is needed for test generation.

## Verification checklist

- [ ] Application launches on a clean Windows 10/11 machine
- [ ] Settings saves API credentials
- [ ] Scan & Analyze works on a sample project
- [ ] Generate Tests reaches the internal API and returns proposals
- [ ] Validate / Apply Approved work for supported languages
- [ ] Diagnostics report shows API endpoint and status
