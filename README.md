# cafeyn-dl

Download a Cafeyn issue from a reader URL and export it as a PDF.

## Install

```bash
uv sync
```

## Configure

```bash
cp .env.example .env
```

Set values in .env:

```env
JWT_TOKEN=...
WEBSESSIONID=...
```

Get them from browser cookies (Cafeyn, while logged in):
- `Cafeyn_authtoken_V2` -> `JWT_TOKEN`
- `Cafeyn_webSessionId` -> `WEBSESSIONID`

## Use

```bash
uv run python main.py https://api.cafeyn.co/ticket/reader/21712793/2258961
uv run python main.py <reader_url> --output-dir output --max-workers 16
```
