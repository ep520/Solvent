# Every bank call checked, not just samples

The case of **Inventx AG** and **Outcept** at the sprintd Innovation Sprint, Zurich, 9 to 11 October 2026.

This repository holds everything a team needs: the brief, the data package and a small test API.

| What | Where |
|---|---|
| The brief | this page |
| Calls and transcripts | [`data/`](data/README.md) |
| Trigger API (optional) | [`server.py`](server.py), described [below](#trigger-api-optional) |

## The problem today

Every phone call between customers and bank advisors is recorded, but only a small part is ever
reviewed. Key staff listen to samples. With thousands of call minutes per day, fraudulent behaviour
such as insider trading mostly goes undetected, on both sides of the line. The calls are in Swiss
German, which is where off-the-shelf speech tools stop working.

## Your task

Build a system that automatically checks every phone call in Swiss German, turns it into a reasoned
compliance alert and knows when it is unsure.

A call comes in as audio. The system transcribes it, runs several kinds of AI-based fraud checks
(for example for insider trading, for sharing access information or for disclosure to third
parties) and decides: **alert**, **review** or **no alert**. On an alert, the compliance officer
receives the suspicious audio snippets with a timestamp and a reason. They want to hear the
suspicious passage, not a 20-minute call. Where the system is unsure, it says so and hands the case
to a human instead of guessing.

In the target picture the checks are integrated into the bank's call recording solution and run
automatically on every call, without anyone uploading anything.

## Mandatory

1. Transcription of the Swiss German call.
2. Several kinds of AI-based fraud checks.
3. Suspicious passages as audio snippets, each with a timestamp and a reason.
4. An adjustable threshold.
5. A metric for false alarms and missed cases, reported on the test set.

The prototype runs without manual steps per call.

## Stretch goals

- Speaker separation: tell customer and advisor apart.
- Notifying the right person on an alert.
- Feedback: mark false alarms, and the system suggests better thresholds.
- Patterns across several calls, such as an advisor who stands out repeatedly.
- An operating concept for running this in production in a secured environment.
- The Outcept trigger API in this repository: your solution reports its hits to it.

## Tuning and configuration

Two different things:

- **Tuning** is how strict the system is: one threshold that trades false alarms against missed cases.
- **Configuration** is what the system looks for: the keyword families and the kinds of checks.

A compliance officer should be able to change both without code. A pure keyword filter is easy to
configure but blind to context. A language model understands context but is hard to steer. The
interesting solutions sit in between.

## The data

In [`data/`](data/README.md): 42 calls (WAV, mono, 16 kHz, Swiss German) from 21 dialogues, each in
a clean and a noisy version, with the script of every dialogue as a transcript.

- **Level 1** (`Stufe1_`): direct keyword calls.
- **Level 2** (`Stufe2_`): context pairs, similar calls where only the context decides.
- **Level 3** (`Stufe3_`): open cases where the expected assessment is "review".

This is 70 percent of the material. Inventx and Outcept hold back 30 percent as a hidden test set.
On Sunday the solutions run on that part, so precision and recall are fairly comparable.

All calls are fictional. There is no real customer or bank data. The transcripts are the scripts the
recordings were made from, not verified transcriptions.

## The rules: if it can be self-hosted, it is allowed

The audio comes from a sensitive banking context, so the solution must be able to run in a secured
environment.

| Allowed | Not allowed for the core pipeline |
|---|---|
| Whisper or Swiss German variants, self-hosted | Pure cloud services without a self-hostable model, such as Deepgram, AssemblyAI or ElevenLabs |
| Open language models such as Llama or Mistral | Real customer or bank data |
| Any model you can run on the GPUs you get | |
| Any programming language and any framework | |

You get GPUs for the weekend. The OpenAI and ElevenLabs credits available to every builder at the
event do not fit this rule: do not build transcription or the fraud checks on them.

## How we judge

| Criterion | Weight | What we reward |
|---|---|---|
| Creativity & innovation | 20 | A fresh, surprising and workable approach. |
| Feasibility | 10 | Realistic architecture that could run in a bank's secured environment, on self-hostable models. |
| Design & usability | 25 | A compliance officer understands each alert and its reason, and can adjust threshold and keyword list without code. |
| Impact & relevance | 20 | Does it solve the actual problem effectively? |
| Detection quality & traceability | 25 | Few false alarms and few missed cases on the hidden test set. Every hit comes with its passage and criterion. The solution shows when it is unsure and escalates instead of guessing. |

## Sunday

- **08:30** final submission.
- **09:00 to 11:00** team pitches: 5 minutes live demo, 3 minutes questions. Slides are optional.

Show in the demo:

- one call from audio to alert, with the suspicious audio snippets,
- one change to the threshold or the keyword list with a visible effect on the number of hits,
- one borderline case: a harmless call that still contains a keyword,
- your detection quality on the test set.

**Prize** for the winning team of the case: goodies from Inventx and Raspberry Pis from Outcept.

## Contact

| Name | Role | Email |
|---|---|---|
| Thaddeus Zambellis | Outcept | thaddeus.zambellis@outcept.ch |
| Bleon Hyseni | Outcept | bleon.hyseni@outcept.ch |

## Trigger API (optional)

A tiny local API for testing. Your application reports a **trigger** whenever it detects something
questionable in a call. The API keeps the triggers in a list and shows them on a page where a click
on a title opens its link in a new tab. Ideally the link leads back into your product, straight to
the suspicious passage with about 10 seconds before and after.

One small Docker container, Python standard library only, no database, no accounts.

### Start

One command, the same in PowerShell, cmd, bash and zsh on Windows, Linux and macOS:

```
docker run --rm -p 8080:8080 ghcr.io/outcept/inventx-case-study
```

Then open <http://localhost:8080>. Stop it with `Ctrl+C`. For another port, change the left side of
`-p`, for example `-p 9000:8080`.

Or build it from source:

```
git clone https://github.com/Outcept/inventx-case-study.git
cd inventx-case-study
docker compose up --build
```

Then open <http://localhost:8080>. Stop it with `Ctrl+C`.

Without Docker, with Python 3.9 or later and nothing to install:

```
python3 server.py        # Windows: py server.py
```

### Port already in use?

The container always listens on 8080 inside. Choose any free port on your machine with `PORT`:

| Shell | Command |
|---|---|
| bash, zsh | `PORT=9000 docker compose up --build` |
| PowerShell | `$env:PORT=9000; docker compose up --build` |
| cmd | `set PORT=9000 && docker compose up --build` |

Without Docker: `PORT=9000 python3 server.py` (PowerShell: `$env:PORT=9000; py server.py`).

### Endpoints

| Method | Path | What it does |
|---|---|---|
| `POST` | `/triggers` | Report a trigger. Answers `201` with the stored trigger. |
| `GET` | `/triggers` | All triggers. JSON for clients; a browser gets the list page. Force one with `?format=json` or `?format=html`. |
| `DELETE` | `/triggers` | Clear the list, handy between test runs. Answers `204`. |
| `GET` | `/docs` | Examples in several languages, using the address you opened it from. |
| `GET` | `/health` | `{"status": "ok"}` |

#### POST /triggers

JSON body:

| Field | Required | Meaning |
|---|---|---|
| `url` | yes | Absolute `http` or `https` link back into your product, ideally straight to the suspicious passage (for example with about 10 seconds of audio before and after). |
| `title` | yes | Short label shown in the list, up to 200 characters. |
| `description` | no | Why this was flagged, up to 2000 characters. |

A missing or invalid field answers `400` with an `error` message and an example body.

```
curl -X POST http://localhost:8080/triggers \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/calls/42?t=95", "title": "Possible insider trading", "description": "Customer wants to buy before the announcement."}'
```

```json
{
  "id": 1,
  "url": "https://example.com/calls/42?t=95",
  "title": "Possible insider trading",
  "description": "Customer wants to buy before the announcement.",
  "created_at": "2026-10-09T18:30:00+00:00"
}
```

#### GET /triggers

```
curl http://localhost:8080/triggers
```

```json
{"count": 1, "triggers": [{"id": 1, "url": "https://example.com/calls/42?t=95", "title": "Possible insider trading", "description": "Customer wants to buy before the announcement.", "created_at": "2026-10-09T18:30:00+00:00"}]}
```

### Examples

The running API serves these at `/docs` as well.

**PowerShell**

```powershell
$body = @{
  url = "https://example.com/calls/42?t=95"
  title = "Possible insider trading"
  description = "Customer wants to buy before the announcement."
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://localhost:8080/triggers" -ContentType "application/json" -Body $body
```

**Python**

```python
import requests

response = requests.post("http://localhost:8080/triggers", json={
    "url": "https://example.com/calls/42?t=95",
    "title": "Possible insider trading",
    "description": "Customer wants to buy before the announcement.",  # optional
}, timeout=5)
response.raise_for_status()
print(response.json())
```

**JavaScript / TypeScript**

```js
const response = await fetch("http://localhost:8080/triggers", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    url: "https://example.com/calls/42?t=95",
    title: "Possible insider trading",
    description: "Customer wants to buy before the announcement.", // optional
  }),
});
console.log(await response.json());
```

**Go**

```go
body, _ := json.Marshal(map[string]string{
	"url":         "https://example.com/calls/42?t=95",
	"title":       "Possible insider trading",
	"description": "Customer wants to buy before the announcement.",
})
resp, err := http.Post("http://localhost:8080/triggers", "application/json", bytes.NewReader(body))
```

**Java**

```java
HttpRequest request = HttpRequest.newBuilder(URI.create("http://localhost:8080/triggers"))
    .header("Content-Type", "application/json")
    .POST(HttpRequest.BodyPublishers.ofString(
        "{\"url\": \"https://example.com/calls/42?t=95\", \"title\": \"Possible insider trading\"}"))
    .build();
HttpResponse<String> response = HttpClient.newHttpClient().send(request, HttpResponse.BodyHandlers.ofString());
```

**C#**

```csharp
using var client = new HttpClient();
var response = await client.PostAsJsonAsync("http://localhost:8080/triggers", new {
    url = "https://example.com/calls/42?t=95",
    title = "Possible insider trading",
    description = "Customer wants to buy before the announcement."
});
```

### Configuration

Environment variables, all optional. Pass them with `-e NAME=value` to `docker run`.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Port the server listens on. With Docker, change the left side of `-p` instead. |
| `HOST` | `0.0.0.0` | Address the server binds to. |
| `MAX_TRIGGERS` | `1000` | Only the newest triggers are kept. |
| `DATA_FILE` | unset | Path of a JSON file to keep the list across restarts. Unset means in memory only. |

Keep the list across restarts with Docker (after `docker compose build`, the image is called `trigger-api`):

```
docker run --rm -p 8080:8080 -e DATA_FILE=/data/triggers.json -v trigger-data:/data trigger-api
```

### Good to know

- **Browser front ends:** cross-origin requests are allowed, so a page on another port can call the API directly.
- **Your app runs in Docker too:** from inside another container, `localhost` is that container. Reach
  the API on your machine with `http://host.docker.internal:8080` (on Linux add
  `--add-host=host.docker.internal:host-gateway` to your container), or put both containers on one
  Docker network and use the container name.
- **Only for local testing.** There is no authentication. Do not expose it to the internet and do not
  send real customer data.

### Development

```
python3 -m unittest discover -s tests -v
```
