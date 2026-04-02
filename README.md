# Claude Usage Monitor

A Windows system tray application that monitors your [Claude.ai](https://claude.ai) usage limits in real time.

## Features

- **System tray icon** — displays your current 5-hour session usage as a number with a color-coded blinking indicator (green / yellow / red)
- **Dashboard popup** — left-click the tray icon to see detailed usage with progress bars
  - 5h Session limit with countdown timer
  - 7d Weekly limit with reset day/time
- **Auto-refresh** — polls usage data every 5 minutes
- **Secure storage** — session key is encrypted with Windows DPAPI (tied to your Windows user account)
- **Single instance** — prevents multiple copies from running simultaneously

## Screenshot

![tray icon](https://img.shields.io/badge/tray-icon%20with%20percentage-blue) ![dashboard](https://img.shields.io/badge/dashboard-dark%20theme-1E1E1E)

<img width="354" height="325" alt="image" src="https://github.com/user-attachments/assets/67862823-e8c1-4e21-b10e-8b41a2de3e34" />

## Download

Go to the [Releases](../../releases) page and download `ClaudeUsageMonitor.exe`. No installation required — just run it.

## Setup

1. Run `ClaudeUsageMonitor.exe`
2. A dialog will ask for your **sessionKey**
3. To get it:
   - Open [claude.ai](https://claude.ai) in your browser and log in
   - Press `F12` → go to **Application** → **Cookies** → `https://claude.ai`
   - Copy the value of the `sessionKey` cookie (starts with `sk-ant-sid`)
4. Paste it into the dialog and click OK
5. The tray icon will appear — you're all set!

## Right-click menu

| Option | Description |
|--------|-------------|
| Show Dashboard | Open the usage popup |
| Refresh Now | Fetch latest usage data immediately |
| Set Session Key... | Update your session key |
| Quit | Exit the application |

## Building from source

### Requirements

- Python 3.10+
- Windows 10/11

### Install dependencies

```bash
pip install -r requirements.txt
```

### Run directly

```bash
pythonw main.py
```

### Build standalone exe

```bash
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name "ClaudeUsageMonitor" --clean ^
  --exclude-module numpy --exclude-module scipy --exclude-module pandas ^
  --exclude-module matplotlib --exclude-module setuptools --exclude-module pkg_resources ^
  --exclude-module distutils --exclude-module jinja2 --exclude-module win32com ^
  --exclude-module pythoncom --exclude-module pywintypes --exclude-module pywin32 ^
  --exclude-module unittest --exclude-module xml --exclude-module xmlrpc --exclude-module test ^
  main.py
```

The exe will be in the `dist/` folder.

## How it works

- Uses the claude.ai web API (`/api/organizations/{id}/usage`) to fetch usage data
- Authenticates via the `sessionKey` cookie from your browser session
- Uses [curl_cffi](https://github.com/lexiforest/curl_cffi) to impersonate Chrome's TLS fingerprint (required to bypass Cloudflare)
- Session key is encrypted at rest using [Windows DPAPI](https://learn.microsoft.com/en-us/windows/win32/secdp/data-protection-api) — only your Windows user account can decrypt it

## License

[MIT](LICENSE) — shadowknife
