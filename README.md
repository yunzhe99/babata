# Babata

Babata connects Rokid smart glasses to your own AI assistant server. It includes the AIUI client, the HTTPS gateway, persistent conversations, server memory, and a photo archive. Each person deploys a fresh instance with their own credentials and data.

[中文说明](README.zh-CN.md) · [Installation guide for coding agents](docs/AGENT_INSTALL.md) · [Glasses client](mobile/rokid-aiui/README.md)

## What it does

- Continuous Chinese voice conversation in the foreground: listen, answer aloud, then listen again after playback ends.
- The server model decides whether a request needs one photo. The client validates the action, takes one foreground photo, and returns it to the same conversation.
- Conversations survive server restarts. The Codex backend can form and search server memory; explicit “记住：…” requests can update the person's profile.
- Accepted photos can be stored as normalized JPEG files with descriptions and a manifest, in a private persistent volume.

Memory formation depends on the backend, model, configuration, and its background processing. An answer, an upload, or a successful HTTP request does not by itself prove that memory was formed or a photo was saved.

## Give this to your coding agent

> Read `AGENTS.md` and `docs/AGENT_INSTALL.md`. Deploy a fresh Babata instance for me, using my own model credentials and data. Prepare my private Rokid build outside the checkout. Verify real model responses, recall after restart, photo files, and isolation from another person's instance. Report which glasses checks still require me to operate the device.

The guide collects the host, domain, model credentials, and person ID before deployment. Source control contains templates only. Private configuration, generated glasses packages, conversations, memories, and photos must stay outside Git.

## International glasses

Use [AIUI Global](https://aiui-global.rokid.com/) with the owner's Rokid account. The project source is `mobile/rokid-aiui`; the agent prepares a private copy with that person's endpoint and access token before importing it. Package and save the project, then use the same account in the Hi Rokid phone app to update glasses resources. See the [official quickstart](https://js.rokid.com/AIUI/guide/quickstart/quickstart?lang=en-US&version=latest).

The published source includes the complete backend. A new person's international glasses, automatic background memory, playback interruption, and exit cleanup still need acceptance on that person's deployment and device. Local tests and Studio previews do not replace this check.

## Source map

| Path | Purpose |
| --- | --- |
| `babata/` | Private assistant, persistent sessions, memory, device actions, and Rokid gateway |
| `mobile/rokid-aiui/` | Rokid AIUI voice and single-photo client |
| `scripts/` | Instance setup and pinned runtime installation |
| `deploy/` | Optional HTTPS deployment files |
| `tests/` | Backend protocol and storage tests |
| `docs/AGENT_INSTALL.md` | Installation, privacy rules, and acceptance checklist |

## Development checks

Use Python 3.12+ and Node.js 22.15+:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
cd mobile/rokid-aiui
npm test
```

These checks exercise local behavior. A deployment also needs live model, HTTPS, persistence, and glasses checks.

## License

[MIT](LICENSE), except the Moby-derived security policies under `deploy/security/`, which retain their [Apache-2.0 license and attribution](deploy/security/NOTICE). Third-party runtimes and services retain their own licenses and terms.
