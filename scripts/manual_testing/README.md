# Manual test scripts

Live end-to-end checks against a running service. Unlike `tests/`, these need real
infrastructure (Postgres, Redis, Azure OpenAI, AI Search, Blob storage).

```bash
# service running on http://localhost:8000
cd scripts/manual_testing
python 00_smoke.py          # probes, login, anonymous access is refused
python 01_auth_rbac.py      # roles, token rotation, cross-user session isolation
python 02_deck_flow.py      # ingest → plan → build → edit → remove → download → delete
python 03_approvals.py      # submit / approve / reject workflow
python 04_apim_image.py path/to/image.png  # APIM Azure OpenAI vision smoke test
python 11_apim_probe.py                     # APIM Foundry: which params/tools each Claude model accepts
python 12_apim_foundry_image.py             # max mode: which image block shape each route accepts
python 13_render_slide.py [--review]        # max mode: LibreOffice slide render timings (+ judge)
python -m scripts.manual_testing.test_ai_video_indexer path/to/video.mp4  # Azure Video Indexer
python 06_demo_walkthrough.py             # narrated end-to-end demo (asserts nothing)
```

Each script exits non-zero on the first failing check, so they chain in CI:

```bash
for s in 0*.py; do python "$s" || exit 1; done
```

## Configuration

| Variable                 | Default                 | Purpose                                   |
| ------------------------ | ----------------------- | ----------------------------------------- |
| `SE_API_URL`             | `http://localhost:8000` | Service base URL                          |
| `SE_SESSION_TOKEN`       | –                       | An existing session handle from `/auth/login` |
| `SE_GRAPH_ACCESS_TOKEN`  | –                       | Entra access token scoped to Microsoft Graph  |
| `SE_ID_TOKEN`            | –                       | Entra id token for this application       |

Sign-in is federated, so the scripts cannot create their own accounts. Set either
`SE_SESSION_TOKEN` (copy the `token` from a browser login) or the token pair to
exchange at `/auth/login`.

`00`, `01` and `03` are fast and need no Azure services. `02` drives the full GenAI
workflow and takes a few minutes. `04` calls APIM directly, so it needs the APIM tenant,
client, secret, scope, endpoint, and subscription-key settings. It accepts PNG, JPEG, and
WebP images and uses the `gpt-4o` APIM Azure OpenAI deployment by default.

`test_ai_video_indexer.py` uploads a local video with the application's production
extractor. It requires `VIDEO_INDEXER_ACCOUNT_ID`, `VIDEO_INDEXER_REGION`, and a current
`VIDEO_INDEXER_ACCESS_TOKEN` in `app/config/dev.env`; the access token is short-lived.
