# Deck Download File Names

This document describes a change to how the SE Agent web app names downloaded PowerPoint
decks. It explains why the change was made, which files were touched, and how the file name is
built from backend data.

## Summary

| Before | After |
|--------|-------|
| `{projectTitle}_v{version}[_{language}].pptx` | `{plan_title}_{projectTitle}_v{version}[_{language}].pptx` |
| `Alpha_project_title_v2_Spanish.pptx` | `Second_plan_Alpha_project_title_v2_Spanish.pptx` |

The new name applies to every download path:

| Download from | Language part |
|---------------|---------------|
| Training Studio → **Outputs** → language button → **Download** | Included (for example `_Spanish`) |
| Content **Pipeline** → **Download** | Not included (the Pipeline does not select a language) |

## Why this change was made

**Every version of a project had the same file-name stem.** File names were built only from the
project title and version number. A project can contain several decks with different content.
Each deck version has its own plan title, for example "Lockout/Tagout basics" or "Advanced
troubleshooting". Downloaded files did not say what was inside them, so files from different
projects or versions were hard to tell apart in a Downloads folder.

**The plan title is the best description of a deck.** The backend stores the title of the
generated deck plan for every version in the `ppt_versions.plan_title` column. Putting it first
makes the file name describe the deck's content. The project title and version that follow
identify where the file came from.

**Project title instead of session ID.** An earlier draft used the internal session ID (for
example `web-mvaclpok`), which means nothing to users. It now uses the project title shown in
the sidebar. Because the title is read when the download starts, a renamed project downloads
under its new name.

## Data sources

| Part of name | Source | Backend table / column | How it reaches the frontend |
|--------------|--------|------------------------|-----------------------------|
| `plan_title` | Version row for the deck version being downloaded | `ppt_versions.plan_title` | `GET /api/decks/{session_id}/versions` |
| `projectTitle` | Project in the local store (`Session.title`) | `user_sessions.title` | `GET /api/decks/sessions`; updated after a rename |
| `version` | The clicked output card or the project's latest version | `ppt_versions.deck_version` | Already present at the call site |
| `language` | The clicked language button | Key in `ppt_versions` files | Already present at the call site |

**No backend change was needed.** The versions endpoint already returned `plan_title`.


## Files changed

| File | Change |
|------|--------|
| [src/app/shared/services/workspace.service.ts](src/app/shared/services/workspace.service.ts) | `deckFileName()` is now `async` and builds `{plan_title}_{projectTitle}_v{version}[_{language}]`. A new `deckPlanTitle()` reads `plan_title` from the cached Studio outputs or from `GET /decks/{sid}/versions`, with `sid` URL-encoded. `onPipelineDownload()` and `downloadDeckVersion()` now await the name before calling `api.downloadDeck()`. |
| [src/app/shared/services/api.service.ts](src/app/shared/services/api.service.ts) | No behavior change. A comment on `downloadDeck()` documents the name format callers pass. The `{sessionId}_v{version}` fallback stays for callers that pass no name. |
## Naming rules

| Situation | Result |
|-----------|--------|
| Normal Studio download | `Second_plan_Alpha_project_title_v2_Spanish.pptx` |
| Pipeline download (no language) | `Beta_plan_v4_Beta_project_title_v4.pptx` |
| `plan_title` has illegal characters (`Safety: Lock/Tag "Out"?`) | `Safety_LockTag_Out_Alpha_project_title_v1_English.pptx` |
| Version has no `plan_title`, or the versions request fails | `Gamma_project_title_v2.pptx` |
| Project renamed to `Beta: renamed` | `Beta_plan_v4_Beta_renamed_v4.pptx` |
| Project title unknown locally | Session ID used in its place, for example `Plan_web-abc123_v1.pptx` |

## Behavior notes

- **The plan title is taken from the downloaded version**, not from the latest version or
  the project. Older versions keep their own plan titles.
- **Studio downloads usually make no extra request**, because the Outputs panel has already
  loaded the version list. Pipeline downloads, and downloads for another project, make one
  `GET /decks/{sid}/versions` request first.
- **The download never fails because of naming.** If the plan title cannot be loaded, that
  part is left out and the download continues.
- Filename cleaning only removes characters that Windows, macOS, and Linux do not allow.
  Other Unicode characters, such as accented letters, are kept.

## How to test

Build and serve the frontend in Docker as described in the README. Then run:

```powershell
docker run --rm -v "${PWD}:/work:ro" -v "${env:TEMP}/se-project-smoke:/artifacts" -v se-analytics-pw:/tmp/pw:ro -e NODE_PATH=/tmp/pw/node_modules -e ARTIFACT_DIR=/artifacts mcr.microsoft.com/playwright:v1.51.1-noble node /work/scripts/download-filename.smoke.cjs
```

The test mocks all API and Blob traffic, so it needs no backend. It checks:

1. Studio output downloads (Spanish and English) use plan title, project title, version, and
   language.
2. Characters that are not allowed in filenames are removed from `plan_title`.
3. A Pipeline download for a project that is not open fetches the version list and uses the
   plan title of the matching version (`v4`, not `v3`).
4. A version with no `plan_title` falls back to `{projectTitle}_v{version}`.
5. After a rename, the download uses the new project title.

To check manually, open a project, expand **Outputs**, choose a language, select **Download**,
and confirm the saved file name. Then do the same from the **Pipeline** tab.
