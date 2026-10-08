# Instructions for installation and development agents

This is a public source repository. Start with `docs/AGENT_INSTALL.md` when asked to deploy it. Work toward one functioning private instance before adding features.

Read `LICENSE` and `LICENSING.md` first. Uses permitted by PolyForm Noncommercial 1.0.0 can proceed under its terms without requesting an additional license. Commercial purposes outside those permissions require a separate written license. Preserve LICENSE and required NOTICE files in distributed source, glasses projects, and images; do not treat the summary as overriding the license's institutional permissions.

## Privacy and isolation

- Use the new owner's credentials, domain, person ID, endpoint token, database volume, Codex state, and photo volume. Create fresh state for each person.
- Never read or import the author's or another person's `.env`, auth files, conversation databases, memories, Skills, photos, device packages, or server state. The public checkout is sufficient.
- Never put secrets or personal data in Git, issue text, build logs, terminal output, screenshots, or acceptance reports. Do not print private configuration or expanded Compose configuration.
- `.env`, runtime volumes, and generated glasses builds are private. A configured AIUI project and its AIX package contain the device token; prepare them outside the checkout and keep them private.
- One Codex home belongs to one person. Changing a Rokid account or a `user_id` alone does not isolate server memory. Use separate Compose projects and fresh volumes.
- Public HTTPS must reach only the authenticated Rokid gateway. Keep the assistant, database, Codex app-server, and memory endpoints private.

## Work from the actual implementation

- Read the current setup script and Compose files before choosing commands or ports. Inspect existing host services before binding a port or altering a reverse proxy.
- Preserve request/session/action binding, single-use camera actions, expiry, image limits, secret masking, and late-event cleanup.
- The client asks the server whether a photo is needed. Do not restore keyword-based camera decisions or background capture.
- `remember:false` means no explicit priority profile update. It does not disable Codex native memory. Memory formation is asynchronous and must be checked separately.
- The default full glasses path uses the Codex runtime. A generic SDK chat endpoint is not evidence that camera decisions or native memory work.
- Additional desktop bridges, personal Skills, and account connectors require separate configuration and authorization. Their source modules do not mean those external services are connected.

## Verification and reporting

- Run relevant local checks after changes, then perform the deployment checks in the guide. Use synthetic, nonsensitive test messages and photos.
- Check real model responses, multiple turns, restart recall, photo bytes plus manifest, and separation of two people's state. A health check or HTTP 200 alone is insufficient.
- Do not claim a new international glasses device passed because Studio preview or unit tests passed. Ask the owner to operate the device when required, and record the observed result.
- Report what works, what was checked, and what remains unverified. Automatic background memory, playback interruption, and exit cleanup require their own evidence.
- Do not broaden the task into a shared multi-user service. The supported deployment recipe is one private instance per person.
