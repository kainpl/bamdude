---
title: API Reference
description: REST API documentation
---

# API Reference

BamDude provides a REST API for integration with external tools and automation.

---

## :material-key: Authentication

### API Key Authentication

Include your API key in the `X-API-Key` header:

```bash
curl -H "X-API-Key: your-api-key" \
  http://localhost:8000/api/v1/printers
```

### Getting an API Key

1. Go to **Settings** > **API Keys**
2. Click **Create API Key**
3. Select permissions
4. Copy the key (shown only once)

---

## :material-web: Interactive API Browser

BamDude includes a built-in API browser at **Settings** > **API Keys** for exploring and testing endpoints.

- Grouped by category (printers, archives, queue, etc.)
- Auto-filled request body examples
- Live execution with formatted responses

---

## :material-api: Core Endpoints

### Printers

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/printers` | List all printers |
| POST | `/api/v1/printers` | Add a printer |
| GET | `/api/v1/printers/{id}` | Get printer details |
| GET | `/api/v1/printers/{id}/status` | Get printer status |

### Archives

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/archives` | List archives |
| GET | `/api/v1/archives/{id}` | Get archive details |
| POST | `/api/v1/archives/{id}/reprint` | Reprint an archive |

### Queue

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/queue` | Get queue status |
| POST | `/api/v1/queue` | Add to queue |
| DELETE | `/api/v1/queue/{id}` | Remove from queue |

### Library

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/library/files` | List files |
| POST | `/api/v1/library/files/upload` | Upload file |

### Metrics

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/metrics` | Prometheus metrics |

---

## :material-code-json: OpenAPI Schema

The full OpenAPI schema is available at:

```
GET /api/v1/openapi.json
```

Use this with Swagger UI, Postman, or any OpenAPI-compatible tool.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
