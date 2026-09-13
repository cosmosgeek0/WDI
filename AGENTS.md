# WDI

The deliverable is the working product. Keep its complete implementation small, clear and fast. Working bloat is rejected.

## Build and simplify

- Finish the owner's requested work in the existing product. Do not turn a cleanup into a replacement application or expand it into other capabilities.
- Read the affected behavior and its dependencies before editing. Understand what must work and what can be removed.
- Build vertically: finish one complete behavior through the product before starting the next. Do not build disconnected pieces and leave integration for later.
- Judge simplicity across the whole working flow: direct steps, plain names, few moving parts and no repeated behavior.
- Reduce code by removing unnecessary work, duplication, intermediate layers and dependencies. Prefer changing or deleting existing code over adding wrappers or parallel implementations.
- Keep one implementation of each behavior and one authoritative calculation of each value. Share code only when real repetition exists and the shared form makes the complete flow simpler.
- Do not split a readable sequence into helpers, classes or files merely to make it look structured. Use the smallest clear form that preserves the required behavior.
- Every addition must serve the requested capability or a concrete requirement or documented failure. Do not add future features, dormant options or speculative architecture.
- Delete replaced behavior and its unused imports, configuration and dependencies in the same change. Do not leave the old path alongside its replacement.
- A smaller implementation must still perform the required work. Moving code elsewhere, compressing readable code or omitting functionality does not establish a reduction. Do not force an arbitrary line target or one-file design.
- Measure performance where it affects cost, responsiveness or scale. Do not claim a limit, necessity or improvement without a stated basis; challenge your own assumptions and recheck disputed claims.

## Divide substantial work

- Do not hand one worker the whole codebase with a vague instruction to simplify it. Identify the real behaviors and dependencies, then give workers bounded, understandable pieces of the existing product.
- Use separate tasks for independent pieces that would pull the main task away from its own work. Do not split tightly coupled changes just to run more workers.
- Keep new-task prompts short: the objective, paths to read (including this file), and essential nuances absent from those files. Let the task inspect the source itself. Do not paste history, repeat file contents or rules, or prescribe an implementation before it investigates.
- Each worker completes its assigned change. The coordinating task keeps its own work moving and integrates completed changes into the working product. Delegation does not replace delivery.
- Use the owner-started task chip. Use sub-agents only for narrow fact lookups, never building, measuring, running or reviewing.
- Do not let workers edit the same file or control the same live process concurrently. Use no more tasks than independent pieces of work.
- Do not create extra tasks for test suites, reports, compulsory reviews or progress chatter. Collect one completed result from each task.

## Do not interrupt active work

- Read the receiving task's latest turns before sending a message. Interrupt only for immediate money risk, an imminent file or host collision, or an urgent owner instruction.
- Hold routine findings, plans and status messages until that task finishes. Send one urgent issue in a few lines; do not repeat messages or chase replies.
- Only the owner can change another task's assignment. Check relayed claims against the actual source or system before acting on them.

## Finish the product

- Carry authorized work through implementation and integration. A plan, analysis, test count, report or handoff does not complete an implementation task.
- Testing supports the product. Use existing checks and the smallest necessary check of the affected behavior against real data and the real app or provider when execution is authorized.
- Do not create test suites, simulation frameworks, benchmark harnesses or report files by default. Do not turn verification into a separate project. Inspect text-only changes directly.
- When the flow fails, find the actual cause, fix it and rerun the affected flow. Keep necessary failure handling and make unresolved errors visible.
- Stop checking when the required behavior is proven. Do not repeat unchanged checks, expand into unrelated hardening or keep collecting evidence after the question is answered.
- Do not invent approval gates, compulsory review chains, fixed reporting templates or handover paperwork. Do not ask again for authorization already given.
- Before finishing, compare the changed flow with a simpler direct implementation. Remove unnecessary structure and your temporary artifacts; preserve unrelated work and real data.
- Return the working result and any unresolved failure briefly in the chat. For simplification, state the actual code reduction and what was removed. Do not substitute an apology or a promise of later work for completing work that is still possible.
- Stop after the requested work is complete. A successful result does not authorize the next capability or a live action outside the requested scope.

## Nodes and evidence

- Give each operational decision one controlling implementation. Record the initiator, reason and instance for every restart or reallocation.
- Diagnose and recover inside the existing container first; preserve its allocation and device identity where possible. Do not replace a node solely because traffic is idle.
- Preserve real measurements, history and source identities. Keep container IDs, public IPs and provider device IDs distinct.
- Derive displayed values from their actual sources. Show stale or missing data, preserve units and scope, and distinguish observations from estimates.
- Do not equate a running process with provider confirmation or earnings. Verify limits, costs and permissions against current evidence.

## Keep these instructions durable

Write rules about how agents work, not about today's code. Keep changing task plans, file layouts, incident diaries and one-off fixes out of the working rules. Do not turn a simple instruction into extra ceremonies or exceptions. Keep only lasting instructions and useful project/access facts here; do not import another project's workflow.

## Project and access

- Repository: https://github.com/cosmosgeek0/WDI/tree/main.
- Work inside `/Users/cosmosgeek/Desktop/WDI`.
- Website: https://wdi.cosmosgeek.org/.
- Salad deployment: https://portal.salad.com/organizations/cosmosgeeketh/projects/default/containers.
- Salad API key portal: https://portal.salad.com/api-key.
- AWS account: `163092837436`.
- AWS region: `ap-south-1` (Mumbai).
- Current server: `i-0c2e48a7ddc2dd010`, SSH alias `mumbai`.
- Old server: `i-0491397473a219afa`, SSH alias `mumbai-old`.
- Current IAM role: `mumbai-clean`.
- Administration: AWS SSM, CLI profile `default`.
- Salad secret: AWS Secrets Manager `wdi/salad-access`.
- Secret fields: `api_key`, `ssh_private_key`, `ssh_public_key`.
- Read Salad credentials on AWS through the instance role.
- Never print, log, commit, or persist plaintext secret values.
- Do not create local copies of Salad credentials.
- Use the owner's connected Chrome session for the relevant account; do not open another login.


## Website operation

- Website source is `website/`; the private Mumbai origin is `/opt/wdi/website`.
- The origin listens only on `/run/salad-ui/ui.sock`. Keep the existing Cloudflare Access issuer/audience and the `cloudflared-salad` peer-UID check. Do not expose a TCP origin or start a second connector as a migration shortcut.
- Retained histories and collector cursors live in `/home/salad/.hg1`; the IP evidence index and lookup cache live in `/var/lib/salad-ip-evidence` and `/var/lib/salad-ip-enrichment`. Preserve these paths and source/group/run identities during deployment.
- App control remains in `grass.service` and the app owners' deployment tools. Website collectors only read Salad inventory/logs and write website data. They must not launch, stop, reallocate, register or refresh app credentials.
- Current Grass and Repocket graph traffic requires application-scoped records. Shared `eth0` counters remain raw observations, never duplicate app traffic. Missing application bytes stay unavailable. Archived container measurements retain their original scope; provider account credits remain separate.
- Use `ssh mumbai` and `ssh mumbai-old` for unprivileged inspection. Root administration uses AWS SSM `AWS-RunShellScript`, profile `default`, region `ap-south-1`; these SSH accounts do not have passwordless sudo.
- Stage source with `rsync -az --exclude=__pycache__ website/ mumbai:/home/ubuntu/wdi-website-source/`. Through SSM, copy it with `rsync -a /home/ubuntu/wdi-website-source/ /opt/wdi/website/`, then run `/opt/wdi/website/install.sh` after restoring the retained data.
- Inspect website services with `systemctl status salad-dashboard salad-ip-evidence salad-ip-enrichment wdi-grass-collector wdi-repocket-collector --no-pager`. Their logs are in `journalctl -u UNIT`; the website deployment command records its initiator, reason and instance in `journalctl -t wdi-website`.
- Snapshot live SQLite databases with Python `sqlite3.Connection.backup`, not a bare copy of a database with an active WAL. Preserve JSON/JSONL histories, deployment metadata and cursor files as well. Encrypt host-to-host archives; verify each restored file against its snapshot checksum and run `PRAGMA quick_check` before starting writers. Standalone WAL snapshots without sidecars must be switched to `journal_mode=DELETE` for the read-only origin; active collectors reopen their own databases in WAL mode.
- Keep the public connector on `mumbai-old` until the owner authorizes cutover. Complete a final data catch-up and coordinate a single collector owner before retiring that host. Public cutover and retirement require the owner's instruction.
