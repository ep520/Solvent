# Trigger API

A tiny local API for testing your solution to the compliance case. Your application reports a
**trigger** whenever it detects something questionable in a call. The API keeps the triggers in a
list and shows them on a page where a click on a title opens its link in a new tab.

One small Docker container, Python standard library only, no database, no accounts.

## Start

The same command works in PowerShell, cmd, bash and zsh on Windows, Linux and macOS:

```
docker run --rm -p 8080:8080 ghcr.io/outcept/trigger-api
```

Then open <http://localhost:8080>. Stop it with `Ctrl+C`.

> The image is built by the workflow in `.github/workflows/docker.yml` on every push to `main`.
> While this repository is private, the image is private too: use "From source" below.

### From source

```
git clone https://github.com/Outcept/trigger-api.git
cd trigger-api
docker compose up --build
```

### Without Docker

Python 3.9 or later, nothing to install:

```
python3 server.py        # Windows: py server.py
```

## Port already in use?

The container always listens on 8080 inside. Choose any free port on your machine on the left side
of `-p`:

```
docker run --rm -p 9000:8080 ghcr.io/outcept/trigger-api
```

With Compose, set `PORT`:

| Shell | Command |
|---|---|
| bash, zsh | `PORT=9000 docker compose up --build` |
| PowerShell | `$env:PORT=9000; docker compose up --build` |
| cmd | `set PORT=9000 && docker compose up --build` |

Without Docker: `PORT=9000 python3 server.py` (PowerShell: `$env:PORT=9000; py server.py`).

## Endpoints

| Method | Path | What it does |
|---|---|---|
| `POST` | `/triggers` | Report a trigger. Answers `201` with the stored trigger. |
| `GET` | `/triggers` | All triggers. JSON for clients; a browser gets the list page. Force one with `?format=json` or `?format=html`. |
| `DELETE` | `/triggers` | Clear the list, handy between test runs. Answers `204`. |
| `GET` | `/docs` | Examples in several languages, using the address you opened it from. |
| `GET` | `/health` | `{"status": "ok"}` |

### POST /triggers

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

### GET /triggers

```
curl http://localhost:8080/triggers
```

```json
{"count": 1, "triggers": [{"id": 1, "url": "https://example.com/calls/42?t=95", "title": "Possible insider trading", "description": "Customer wants to buy before the announcement.", "created_at": "2026-10-09T18:30:00+00:00"}]}
```

## Examples

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

## Configuration

Environment variables, all optional. Pass them with `-e NAME=value` to `docker run`.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Port the server listens on. With Docker, change the left side of `-p` instead. |
| `HOST` | `0.0.0.0` | Address the server binds to. |
| `MAX_TRIGGERS` | `1000` | Only the newest triggers are kept. |
| `DATA_FILE` | unset | Path of a JSON file to keep the list across restarts. Unset means in memory only. |

Keep the list across restarts with Docker:

```
docker run --rm -p 8080:8080 -e DATA_FILE=/data/triggers.json -v trigger-data:/data ghcr.io/outcept/trigger-api
```

## Good to know

- **Browser front ends:** cross-origin requests are allowed, so a page on another port can call the API directly.
- **Your app runs in Docker too:** from inside another container, `localhost` is that container. Reach
  the API on your machine with `http://host.docker.internal:8080` (on Linux add
  `--add-host=host.docker.internal:host-gateway` to your container), or put both containers on one
  Docker network and use the container name.
- **Only for local testing.** There is no authentication. Do not expose it to the internet and do not
  send real customer data.

## Data package

The calls and transcripts for the case are in [`data/`](data/README.md).

## Development

```
python3 -m unittest discover -s tests -v
```
