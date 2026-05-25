# RD Tracker

Relative density workout tracker. Logs sessions, calculates RD (strength indexed to bodyweight), graphs the trend over time, and exports to Excel.

## Stack

- **Frontend**: Plain HTML/JS + Chart.js + SheetJS, served by nginx
- **Backend**: FastAPI (Python), SQLite database
- **Infrastructure**: Docker Compose

## Quick start (local)

```bash
git clone https://github.com/YOUR_USERNAME/rd-tracker.git
cd rd-tracker
docker compose up --build
```

Open **http://localhost** in your browser.

Data persists in `./data/sessions.db` (mounted as a Docker volume).

## Running on a server / home lab

Same command. If you want a different port, edit `docker-compose.yml`:

```yaml
ports:
  - "8080:80"   # change 8080 to whatever you want
```

Then access via `http://YOUR_SERVER_IP:8080`.

## GitHub Container Registry (optional)

If you want to pull images instead of building:

```bash
# Build and push
docker build -t ghcr.io/YOUR_USERNAME/rd-tracker-backend:latest ./backend
docker build -t ghcr.io/YOUR_USERNAME/rd-tracker-frontend:latest ./frontend
docker push ghcr.io/YOUR_USERNAME/rd-tracker-backend:latest
docker push ghcr.io/YOUR_USERNAME/rd-tracker-frontend:latest
```

Then update `docker-compose.yml` to use `image:` instead of `build:`:

```yaml
services:
  backend:
    image: ghcr.io/YOUR_USERNAME/rd-tracker-backend:latest
    ...
  frontend:
    image: ghcr.io/YOUR_USERNAME/rd-tracker-frontend:latest
    ...
```

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/sessions` | All logged sessions |
| POST | `/api/sessions` | Save/update a session |
| DELETE | `/api/sessions/{date}` | Delete a session by date |
| GET | `/api/trend` | Weekly trend data (seed + logged sessions merged) |

## Project structure

```
rd-tracker/
├── docker-compose.yml
├── frontend/
│   ├── Dockerfile
│   ├── nginx.conf
│   └── index.html
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── main.py
└── data/           ← gitignored, SQLite lives here
```
