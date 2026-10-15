# Qwen Local Proxy

**Language:** [Русский](README.md) | English

A local proxy for **chat.qwen.ai** with an OpenAI-compatible API. The transport is browser-based: the proxy drives a Chrome profile (Selenium, profile `browser_profile/`) and talks to the SPA's own API, so no manual cookie hand-over and no anti-bot fingerprints are needed — you sign in once, right inside the proxy's Chrome window.

No API key is needed for chat: the proxy rides on your own logged-in Chrome.

## Quick start

```
setup.bat   # venv + dependencies + creates .env from .env.example
start.bat   # API server on http://127.0.0.1:8000
gui.bat     # chat GUI (works through the same server)
```

The batch files work from any folder — each one switches into the project directory on its own.

The first request (or the **«Проверить»** button in the GUI, i.e. "Check") opens a Chrome window with the proxy profile — log in to chat.qwen.ai **once**: the session is stored in `browser_profile/` and survives server and GUI restarts.

To check that the server is alive and authorized:

```
curl http://127.0.0.1:8000/v1/health
```

The first call starts Chrome and can take ~2 minutes; after that it answers instantly.

## Configuration: what goes where in `.env`

The `.env` file lives **in the project root**, next to `setup.bat`. You don't have to create it manually: on its first run `setup.bat` copies `.env.example` to `.env`, and an existing `.env` is **never touched** — your edits after `git pull` are preserved.

Values are read by `src/config.py` when the server starts and loaded into the process, so the GUI and `os.environ` see them identically. All keys:

| Key in `.env` | What it does | Default |
|---|---|---|
| `QWEN_BASE_URL` | The site the browser transport automates | `https://chat.qwen.ai` |
| `API_HOST` | The address the proxy listens on. Keep it `127.0.0.1`: the proxy has no authorization, `0.0.0.0` or an empty value would expose it to the whole local network | `127.0.0.1` |
| `API_PORT` | The proxy port | `8000` |
| `QWEN_BROWSER_PROFILE` | The Chrome profile that holds the chat.qwen.ai login. A relative path is resolved from the project root | `./browser_profile` |
| `QWEN_CHROME_PATH` | An **absolute path to `chrome.exe`**, taken as is — there is no auto-discovery. If Chrome is not installed in the default location, edit this line | `C:\Program Files\Google\Chrome\Application\chrome.exe` |
| `QWEN_BROWSER_HEADLESS` | `true` — no window. The first login must happen with a window (`false`), and attachments also work more reliably with a window | `false` |
| `QWEN_BROWSER_LANGUAGE` | Browser language, it affects the site's interface | `ru-RU` |
| `QWEN_PAGE_TIMEOUT` | Seconds to wait for the page/SPA | `90` |
| `QWEN_SEND_TIMEOUT` | Seconds for a full answer (long streaming, file reading) | `300` |
| `DEFAULT_MODELS` | What `/v1/models` and the GUI list return until the site's live list has been read. Comma separated, ids exactly as in the site's picker | `qwen3.7-plus,qwen3.8-max,qwen3.8-omni-flash` |
| `LOG_LEVEL` | Server log level | `INFO` |
| `DEBUG_API` | `true` — raw Qwen requests/responses in the console | `false` |
| `SPA_HEADER_VERSION` | The version header that the SPA itself sends on API reads; the transport copies it. Change it only if API reads start failing after a site update | `0.3.16` |
| `DASHSCOPE_API_KEY` | **Optional**, only needed for `/v1/images/*` (DashScope is a separate service, nothing to do with the chat). Commented out in `.env.example` | — |

Printing a real `DASHSCOPE_API_KEY` in `.env.example` is neither needed nor wise — git should only ever get the template with that line commented out.

Read the values without opening the file by hand:

```
venv\Scripts\python.exe -c "from src import config; print(config.API_PORT, config.QWEN_CHROME_PATH)"
```

**Do not commit** `.env` and `browser_profile/`: the profile holds a live login to your account (Windows-encrypted cookies bound to this machine), and `.env` holds your local paths. Both are already in `.gitignore`.

## API Endpoints

Base: `http://127.0.0.1:8000`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/models` | Model list (live from the site, fallback to `DEFAULT_MODELS`) |
| POST | `/v1/chat/completions` | Chat (OpenAI format, SSE streaming) |
| POST | `/v1/chat` | The same, native format `{message, model, chatId, parentId}` |
| GET | `/v1/health` | Authorization/browser status; the first call starts Chrome, on failures it answers `200 status:"degraded"` |
| POST | `/v1/chat/reset` | Session reset — the next message will start a new chat |
| GET | `/v1/chats` | The account's chat list |
| GET | `/v1/chats/{id}/messages` | Remote history (best-effort; the primary source is the local `.md` transcripts) |
| POST | `/v1/chats/{id}/load` | Make a chat the current session (parentId is restored from the sidecar file) |
| POST | `/v1/files/getstsToken`, `/v1/files/upload` | `501` — the browser transport has no upload API; attachments are passed inline in the request's `files` field |
| POST | `/v1/images/generations` | Image generation through DashScope (requires `DASHSCOPE_API_KEY`) |
| GET | `/v1/images/models` | Generation model list (same key) |

The image-generation input field was removed from the GUI — the endpoints themselves are still there and reachable by any HTTP client.

### Sample request

```
curl http://127.0.0.1:8000/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"qwen3.8-max\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}],\"stream\":true}"
```

### Conversation continuation parameters

`POST /v1/chat/completions` accepts:
- `chatId` / `chat_id` — the ID of the chat to continue
- `parentId` / `parent_id` — the ID of the previous answer (response_id)
- `newChat` / `new_chat` — force starting a new chat
- `files` — inline attachments, see the next section

The last SSE chunk before `[DONE]` is the meta object `{chatId, parentId}`; the server always returns a valid `chatId`.

### Attachments (files in a message)

The SPA only accepts files in the chat input field, it has no upload API of its own. That is why attachments are passed inline in the request body, and the proxy feeds them into the input field through the very same drag-and-drop channel the page itself uses:

```json
{
  "model": "qwen3.8-max",
  "messages": [{"role": "user", "content": "What is the total? (see the file)"}],
  "stream": true,
  "files": [
    {"fileName": "total.txt", "fileType": "text/plain", "fileSize": 12,
     "fileData": "<base64 of the file content>"}
  ]
}
```

The server unpacks the base64 into temporary files, the proxy drops them into the input field, **waits for the attachment cards to appear and only then types and sends the text**. An unreadable, empty or oversized file is skipped with a hint; if the SPA never shows a card, the turn goes out as text with a warning (a file that silently disappears is worse than a noticeable problem). Temporary files are deleted in the stream's `finally` block.

Limits and behavior:

| What | Value |
|---|---|
| Single file size | 15 MB in original bytes (base64 inflates the request body by roughly 1.33x) |
| Documents per message | **5** — exactly as many as the site's input field accepts: with a sixth `.txt` the card no longer appears |
| Images | do **not** count toward the limit of 5 (measured: 4 documents + 4 images = 8 cards) |
| More than 5 documents | `413` with the files listed in `detail` — an honest rejection is better than an answer about "the first five" files you never agreed to |
| File type | Determined from `fileType` (MIME), or from the name's extension when it is missing |

The GUI mirrors these limits and refuses before the request is even sent. The **«Прикрепить»** ("Attach") button adds one file per click — you can click it several times, the list is shown next to the button. The `gui/api_client.upload_file` method (a POST to a 501) is kept for compatibility, but the GUI no longer uses it.

### Turn progress: SSE comments

A turn with attachments is not instant: the file lands in the input field, the site draws a card, reads the content, and only then the text goes out. So that the client does not hang in silence, the server sends progress as **SSE comments** — lines starting with `:`, valid for the protocol and invisible to a regular OpenAI parser (the SDK silently ignores them):

```
: progress Файл total.txt передан в поле ввода (1/2)
: progress Квен прочитал файлы за 6 с
: progress Отправляю сообщение
data: {"choices":[{"delta":{"content":"Total "}}]}
```

The phase strings are sent by the server in Russian — they are wire strings, so they stay as they are (English gloss in parentheses). Full set of phases: `Открываю страницу...` ("Opening the page...") → `Готовлю поле ввода...` ("Preparing the input field...") → `Передаю файл N в поле ввода...` ("Handing file N to the input field...") → `Файл ... передан в поле ввода (N/M)` ("File ... handed to the input field (N/M)") → `Сайт читает файлы: занято N из M карточек` ("The site is reading the files: N of M cards ready") → `Квен прочитал файлы за N с` ("Qwen read the files in N s", or the countdown `Файлы готовы, отправлю через N с` — "Files are ready, will send in N s" — when the cards settle faster than the lower waiting bound) → `Отправляю сообщение` ("Sending the message") → `Квен ответил за N с, собираю ответ` ("Qwen answered in N s, collecting the answer") → `Получено N символов` ("Received N characters"). Between phases the server keeps the connection open with `: ping` comments.

This is how a turn looks both in the GUI (these lines land in the **«Системный лог»** field, i.e. "System log") and for any raw client:

```python
with requests.post(url, json=payload, stream=True, timeout=None) as r:
    for line in r.iter_lines(decode_unicode=True):
        if line.startswith(": progress "):
            print("progress:", line[len(": progress "):])
        elif line.startswith("data: "):
            ...  # answer chunks
```

### Model Aliases

Current models: `qwen3.7-plus`, `qwen3.8-max`, `qwen3.8-omni-flash`.

- Short: `plus`, `max`, `omni`, `omni-flash`, `27b`
- Legacy → current: `qwen-plus`, `qwen-turbo`, `qwen3-plus`, `qwen3.6-plus`, `qwen3.6-27b`, `qwen3.5-plus`, `qwen3.5-27b` → `qwen3.7-plus`; `qwen-max`, `qwen3-max`, `qwen3.6-max`, `qwen3.6-max-preview`, `qwen3.5-max` → `qwen3.8-max`

Unknown names are passed through to the site as is.

### Session persistence

After every completed message the server overwrites the sidecar `logs/.session-<chat_id>.json` (`chat_id` + the last `parentId`). That is why a restarted proxy/GUI can continue any chat without the client resending the history.

**Conversation continuation pattern:**
1. First request → the server creates a chat and answers `{chatId: "A", parentId: "B"}`
2. Second request with `{chatId: "A", parentId: "B"}` → a new `parentId: "C"`
3. Third request with `{chatId: "A", parentId: "C"}` — the context is preserved

## GUI (PySide6)

GUI captions are Russian by design; the server and its logs write English to themselves and the GUI shows them as they are. Below are the exact captions shown in the window, with an English gloss in parentheses. The window is split into zones: top bar, system instruction, attachments, chat + system log, input field.

- **Top bar** — the model list and its ⟳ («Обновить список моделей», refresh the model list), **«Чат:»** ("Chat:") with the chat list, **«Загрузить»** ("Load" — load the selected chat and its history) and its own ⟳, the proxy connection state (`Подключение...` → `Подключено` / `Отключено`, i.e. Connecting... → Connected / Disconnected), the Qwen session state in the browser profile (`сессия: ?` → `сессия: проверяю...` / `сессия: активна` / `сессия: неактивна` / `сессия: сервер молчит`, i.e. session: ? / probing... / active / inactive / server is silent) and the **«Проверить»** ("Check") button — it polls `/v1/health`. The first **«Проверить»** call starts Chrome, wait up to ~2 minutes
- **«Система:»** ("System:") — an optional system instruction, sent with every message
- **«Файлы:»** ("Files:") and **«Прикрепить»** ("Attach") — pick a file (one per click, "it will go with the next message"), the selected files and the document counter `N/5` are shown next to it
- **«Переписка»** ("Chat transcript") — the dark chat window with Markdown rendering and turns labeled «Вы» / «Ассистент» ("You" / "Assistant"); in the header on the right — **«Скопировать ответ»** ("Copy reply", the last answer) and **«Новый чат»** ("New chat"). Every message has its own **«копировать»** ("copy") link. **«Новый чат»** clears the window, drops the selected files and starts a new local session; the sidecar files of older chats are left alone — they can still be continued via **«Загрузить»**
- **«Системный лог»** ("System log") — a separate field under the chat: all service lines land there (progress phases, warnings, errors like `[Ошибка] …`, i.e. "[Error] …"), while the chat keeps only what Qwen said. Monospace font, color by line type, a 1200-line ceiling, vertical scrolling without horizontal scrolling (wide lines wrap and fit in the window), and **«Копировать лог»** / **«Очистить лог»** ("Copy log" / "Clear log") in the header
- The chat and the log are separated by a draggable splitter: the log can be stretched to half the window or collapsed
- Sending with **Ctrl+Enter** (the **«Отправить»**/"Send" button works too)
- Under the input field there is a live turn line: `Жду первый токен — N с (холодный старт запускает Chrome, до ~2 мин)` ("Waiting for the first token — N s, a cold start launches Chrome, up to ~2 min"), then `Думает — последние данные сервера N с назад (всего M с)` ("Thinking — last server data N s ago, M s in total"), the server's progress phases with the total turn time, the warning «возможно, зависло, проверьте окно Chrome» ("it may have hung, check the Chrome window") when the pause runs longer than a normal turn, and `Оборот не удался — ответа нет. Подробности в системном логе ниже` ("The turn failed — there is no answer. Details in the system log below") when a turn never produced an answer
- The status bar shows the current `chatId` and `parentId` (while no chat is selected — `чат не выбран` / `родитель не выбран`, i.e. no chat selected / no parent selected)
- The first send into a new chat is slower than the rest — that is Chrome's cold start, not an error

## Architecture

```
chat.qwen.ai SPA  ⇄  the proxy's Chrome profile (Selenium, browser_profile/)
                                ⇄  the SPA transport (fetch interception over CDP)
                                          ↓
                              qwen_client.py (src/client)
                                          ↓
              routes.py (FastAPI, /v1)  ⇄  GUI (PySide6) / any OpenAI client
```

## Project structure

```
qwen-local-proxy/
├── src/
│   ├── api/          # routes.py, schemas.py, server.py, session_store.py, image_generation.py
│   └── client/       # qwen_client.py — the browser transport to chat.qwen.ai
├── gui/
│   ├── app.py        # GUI (PySide6)
│   └── api_client.py # HTTP client of the local API + transcript logger
├── scripts/          # run_server.py, verification tests and probe harnesses ("For development")
├── logs/             # chat .md transcripts + sidecar .session-<chat_id>.json (created on the fly)
├── browser_profile/  # Chrome profile with the chat.qwen.ai session (created on the fly, do not commit)
├── .env.example      # configuration template → setup.bat copies it to .env
├── setup.bat         # venv + dependencies + .env
├── start.bat         # API server
├── gui.bat           # GUI
├── requirements.txt  # lower version bounds
└── LICENSE           # MIT
```

## Requirements

- Windows
- Python 3.10+
- Google Chrome (browser transport; the path is set in `.env` — `QWEN_CHROME_PATH`)

## Portability to another PC

The repository is cloned and started with a single `setup.bat`. What you should know about moving it:

- **`venv` does not move.** It is tied to the interpreter that created it, so `setup.bat` does not trust the folder's existence — it asks the folder whether it can actually run: a dead venv produces a clear hint "delete venv and run setup.bat again" instead of a silent install into the global Python
- **`.env` is machine-dependent** (Chrome path, profile) — that is why it is not in the repository at all; it is created from `.env.example`
- **`browser_profile/` does not move** — the login is encrypted by Windows and bound to the machine. On each new PC you log in once
- The batch files look for Python in `PATH` first, then via `py -3` — a Python installed without the "Add to PATH" checkbox works as well

## For development

Scripts live in `scripts/` and are run from the project root with the venv interpreter (`venv\Scripts\python.exe scripts\...`). Almost all of them start Chrome with the proxy profile, and the profile allows **only one process at a time**: the server and a verification script must not run at the same time, and port 8000 must be free. Each script prints its own verdict (`RESULT:` / `OK` / `ALL PASS`) — that is what to go by, not the cmd exit code (there `pause` and `findstr` distort the exit code, and the Windows console mangles Cyrillic in output: read logs with an editor, not `type`).

The GUI test is offscreen, does not touch Chrome and is safe to run while the server is running:

```
venv\Scripts\python.exe scripts\test_gui_refresh_offscreen.py   # 72 GUI checks → «RESULT: ALL PASS»
```

Integration checks require a live server (`start.bat`) and a real account:

```
venv\Scripts\python.exe scripts\test_e2e.py
venv\Scripts\python.exe scripts\test_sequential.py
venv\Scripts\python.exe scripts\test_chats_retry.py
venv\Scripts\python.exe scripts\probe_docs_stream.py six    # 6 documents → expect 413, answer not started
venv\Scripts\python.exe scripts\probe_docs_stream.py fat    # one fat .txt → numbers from the file + progress phases
venv\Scripts\python.exe scripts\probe_docs_stream.py five   # five documents → all five check numbers
```

`probe_docs_stream.py` prints every raw line of the SSE stream: `PROGRESS <text>` for progress comments, `comment   : ping` for keep-alive, `token` for answer deltas, `meta` for the meta chunk and the assembled `ANSWER`. It is the way to see how a turn looks to somebody else's OpenAI client without touching the GUI. The rest of `scripts/` are historical verification scripts written for a specific fix (attachment DOM, send-button geometry, input-field limits); they are not part of the regression set.

## License

MIT — see [LICENSE](LICENSE).
