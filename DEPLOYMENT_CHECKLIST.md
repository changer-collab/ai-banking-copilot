# Online Deployment Checklist

This is the entry point for every Huawei Cloud and Cloudflare deployment.

Read the full checklist before deploying:

- [`docs/2026-10-27/公网Demo-部署单.md`](docs/2026-10-27/公网Demo-部署单.md)
- The daily deployment checklist is in section 11.
- The current tunnel address and change history are in section 13.

## Rules

- Deploy only the `aib-*` services. Do not run a global `docker compose down`.
- A Cloudflare quick-tunnel URL changes when the tunnel restarts. Update the Feishu callback URL and record the new address in the full checklist.
- Do not store credentials, tokens, or private keys in this repository.
- Before and after deployment, run the health and served-page checks described in the full checklist.
- Treat addresses and version notes in historical documents as snapshots; verify the current server and tunnel state before use.

