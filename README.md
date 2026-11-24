# ocr-review

This Python receiver runs [OpenCodeReview](https://github.com/alibaba/open-code-review)
on signed Gitea pull request events and posts one review summary per reviewed commit.
It is a standalone integration and does not require Gitea Actions. It does
not approve, merge, or reopen pull requests.

## Requirements

- Python 3.10+, Git, and OpenSSH on a machine that can reach Gitea and the
  configured model endpoint. Run it on a workstation or a VM; the receiver
  needs no third-party Python packages.
- OpenCodeReview Native v1.14.1, the version used for validation. Download
  the release for your OS and architecture and verify its published checksum.
- A dedicated service account for unattended operation, with read-only Git
  SSH access to the registered repositories. Verify the Gitea SSH host key
  out of band and install it in that account's `known_hosts`. Verify Git
  access with `git ls-remote` as the service account.

## Register repositories

There is no repository registry on the receiver. To onboard a repository,
configure its Gitea webhook to use the shared `/gitea` endpoint and signing
secret, and grant the bot access if it does not already have it.

1. Configure an approved provider, model, and credential in OCR for the account
   running the receiver. The receiver does not override the provider or model.
   Run `ocr llm test` as that account. The model receives the reviewed code.
   Run `ocr config set language Korean` as that account. OpenCodeReview's
   system prompt otherwise says "Always respond in English", which overrides
   the receiver's request for Korean findings.
2. Give the Gitea bot access only to enrolled repositories. Use `read:repository`
   for pull request lookup and `write:issue` for summary comments. Verify native
   review permissions separately before relying on inline comments. Keep
   the token and signing secret files readable only by the service account
   and its administrator.
3. Run the receiver as `ocr-review`. Use absolute paths for the script,
   OCR binary, and credential files. Use the HTTPS address of your Gitea
   instance and the SSH base from its clone URLs.

   ```sh
   OCR_GITEA_URL=https://gitea.example.org \
   OCR_GIT_SSH_BASE=ssh://git@gitea.example.org:2222 \
   OCR_BIN=/path/to/ocr \
   OCR_BOT_LOGIN=ocr-review-bot \
   OCR_WEBHOOK_SECRET_FILE=/path/to/webhook-secret \
   OCR_GITEA_TOKEN_FILE=/path/to/bot-token \
   python3 /path/to/scripts/ocr_webhook.py --port 8765
   ```

4. Proxy `/gitea` from an internal HTTPS address to `127.0.0.1:8765`. Limit
   access to Gitea's network source. In Gitea, open the test repository's
   **Settings → Webhooks**. Add a **Gitea** webhook with that HTTPS URL and
   select only **Pull Request** (`pull_request`) and **Pull Request Synchronized**
   (`pull_request_sync`) under custom events. Generate the receiver's shared secret once
   (`openssl rand -hex 32`) and keep it in the configured file, outside the
   checkout. Set the same secret on each managed Gitea hook. Adding a hook
   does not require a receiver configuration change or restart.
5. Open a same-repository pull request with synthetic code. Look for the bot's
   👀 reaction below the pull request description during review. The bot
   removes the reaction when the review ends and posts one Markdown summary
   for the reviewed commit. Check that Korean findings show the impact and
   suggested fix, with the English text folded below. A verified source line
   opens in a new tab. When the base commit has
   `.opencodereview/review-guide.md`, each `[근거: G8 테스트]` item in a finding
   links to that item's heading in the base-commit guide. Up to three high or critical findings on verified
   added lines can also appear on the native pull request diff. The summary
   retains findings when inline publishing is blocked or uncertain. A partial,
   skipped, or failed review is distinct from a completed review with no
   findings. Push a second synthetic commit and check that the bot posts a
   new summary for that commit, leaves the earlier one in place, and does
   not repost the same inline finding. A retry for the same commit updates
   its existing summary instead of adding another.

The webhook does not execute scripts from a pull request. The shared signing
secret authenticates the installation, not individual repositories. Keep it
with trusted hook administrators only. Do not share a secret between untrusted
administrative groups. Limit the bot's access and restrict ingress to Gitea.

When upgrading from `OCR_WEBHOOK_SECRETS_FILE`, replace it with
`OCR_WEBHOOK_SECRET_FILE`, configure each managed hook with the new shared
secret, and restart the receiver once to load the new configuration.

## Run at boot on Linux

Install the receiver and its virtual-machine configuration in permanent paths.
For example, use `/opt/ocr-review` for the receiver code and `/etc/ocr-review`
for service configuration. Keep the credential files readable only by the service
account and its administrator. Set `HOME` to the account's actual home so OCR
uses that account's existing provider and model configuration.

Save this example as `/etc/systemd/system/ocr-review.service`, substituting the
actual account and paths. Put the six `OCR_*` settings from the command above
in `/etc/ocr-review/receiver.env`, one `NAME=value` per line, without shell
continuations. Put secrets in the files referenced by those settings, not in
the unit or the environment file.

```ini
[Unit]
Description=OpenCodeReview webhook receiver for Gitea
Wants=network-online.target
After=network-online.target

[Service]
User=ocr-review
Environment=HOME=/home/ocr-review
EnvironmentFile=/etc/ocr-review/receiver.env
ExecStart=/usr/bin/python3 /opt/ocr-review/scripts/ocr_webhook.py --port 8765
Restart=on-failure
RestartSec=3s
NoNewPrivileges=yes
UMask=0077

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemd-analyze verify /etc/systemd/system/ocr-review.service
sudo systemctl daemon-reload
sudo systemctl enable --now ocr-review.service
systemctl is-enabled ocr-review.service
systemctl is-active ocr-review.service
```

Keep the receiver on loopback. When an HTTPS proxy on another host needs a
VM listener, persist the socket and its source-IP firewall rule as well. Make
the socket require and start after the firewall unit so it does not listen
when the rule fails to load. Allow only the proxy's backend source address at
the listener, and restrict ingress to Gitea at the HTTPS proxy. Verify both an
authenticated delivery and rejection of unsigned requests after a restart.

## Verify and switch to automatic reviews

- Run `python3 -m unittest discover -s tests` locally. For a delivery-only
  check, use a sandbox-only bot and a separate signing secret, and start the
  receiver with `--comment-smoke`. This posts a test summary without sending
  source to a model.
- The receiver binds to loopback. If Gitea runs on a different machine, put an
  HTTPS reverse proxy in front of it and restrict inbound traffic to Gitea's
  network source. An authenticated test-delivery `204` only confirms transport
  and signing; a qualifying PR delivery returns `202` when queued, not when
  review is finished.
- For unattended use on macOS, run the same command under launchd with the
  service account's home, absolute paths, and restart-on-failure behavior.
- Validate signed delivery on a synthetic repository before switching. Remove
  `--comment-smoke` from the service command to enable model calls, restart the
  service, and use a new synthetic PR to verify delivery, model output, and
  comment updates end to end. Register production repositories only after
  these checks pass. Closed or already merged PRs are not replayed.
- Roll back by disabling the affected Gitea webhooks or stopping the receiver.
  Restoration of the previous service must include its matching configuration.
- Before publishing this repository, review both files and Git history. Do not
  include real signing secrets, tokens, local session state, private PR
  fixtures, or host-specific configuration. Choose and add a license before
  distributing it as open source.