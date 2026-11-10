# SUDO

A personal AI computer operator for Windows. You tell it what you want in plain words. It works on your real desktop, browser, terminal and files, checks the result, and asks you before anything final.

Everything runs on your PC. Only the text and screenshots of a task go to the AI provider you choose.

## Start

1. Double-click `run.bat`.
2. Open **Settings**. Choose a provider and press **Save**. Choosing OpenAI selects `gpt-6.1-terra` automatically.
3. Paste your paid API key and press **Save key**.
4. Type a command on the **Operate** tab.

To stop SUDO: press **Stop**, say `stop` in your own words (`please stop now`, `cancel everything`, `ruko`), press **Ctrl+Alt+Q** from anywhere, or slam the mouse into a screen corner.

SUDO controls Chrome, Edge and Brave directly. Other browsers, such as Firefox, work through the mouse and keyboard. It reads PDF, Word and Excel, and edits Word and Excel. PDF text cannot be rewritten, but pages can be extracted and combined.

Set up on a new machine: `python -m venv .venv`, then `.venv\Scripts\pip install -r requirements.txt`.

## Using your own browser

By default SUDO uses its own separate browser window. To let it use your everyday browser with your open tabs and logins, install the extension once:

1. Go to `chrome://extensions` and turn on **Developer mode**.
2. Press **Load unpacked** and choose the `browser_extension` folder.
3. Open the SUDO Browser Link icon and paste the pairing code and port from SUDO's **Settings** tab.

Your browser shows a bar while SUDO is controlling a tab. Approvals still apply. Remove the extension to cut the link.

## What it asks you first

| It does this alone | It asks first | Only you can do this |
|---|---|---|
| Reading pages, files and the screen | Sending, submitting, posting, buying | Passwords and card numbers, through a masked box the AI never sees |
| Typing into fields and preparing work | Deleting (to the Recycle Bin) and overwriting (with a backup) | CAPTCHAs and MFA codes |
| Running ordinary commands | Installing software, ending programs, pushing to git | Firewall, antivirus and other security settings |

These checks are pattern matches, not a sandbox. SUDO runs with your Windows permissions, and a command worded in an unusual way may not be recognized. The **Cautious** setting asks before any command not known to be read-only.

## How it reports

A task says **VERIFIED** only if machine checks passed, such as the file exists or the field holds the text. **NOT VERIFIED** means the work could not be machine-checked. The model's own claim never counts.

## Layout

| Path | Purpose |
|---|---|
| `sudo_agent/agent.py` | Supervisor loop, task queue, stop hotkey |
| `sudo_agent/tools/` | Computer, browser, terminal, files, research, system tools, and the single gate all actions pass through (`base.py`) |
| `sudo_agent/policy.py` | What runs, what asks, what is refused |
| `sudo_agent/verify.py` | The checks behind VERIFIED |
| `sudo_agent/llm.py` | AI providers. Add a new one here |
| `sudo_agent/memory.py` | Task history and long-term memory (SQLite) |
| `sudo_agent/server.py`, `ui/` | Local API, locked to this PC, and the interface |

Your data lives in `data/` and is never committed: the database, logs, backups, downloads, and the separate browser profiles SUDO uses. Log in to sites once in SUDO's browser window and the login stays.

## Tests

`python -m pytest` runs the core suite with no side effects.

`python -m pytest tests/live -m live` drives your real mouse, keyboard, Chrome and Calculator for a couple of minutes, so leave the PC alone while it runs.
