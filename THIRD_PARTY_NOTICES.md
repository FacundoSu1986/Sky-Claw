# Third-Party Notices

## Hermes Agent

Sky-Claw includes design ideas and protocol-level inspiration from
[Hermes Agent](https://github.com/NousResearch/hermes-agent), developed by
Nous Research.

In particular, Sky-Claw's LLM tool-calling compatibility layer, including
XML `<tool_call>` handling, was informed by conventions used by Hermes Agent.
Sky-Claw is an independent implementation. The current repository audit did
not identify source code copied or directly adapted from Hermes Agent, so no
Hermes source files are redistributed by this repository and no separate
Hermes license file is included at this time.

Hermes Agent:

- Project: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- Copyright: © 2025 Nous Research
- License: [MIT](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE)

Sky-Claw is not affiliated with, endorsed by, or maintained by Nous Research.

## Wrye Bash (experimental headless sidecar)

Sky-Claw integrates with [Wrye Bash](https://github.com/wrye-bash/wrye-bash)
for Bashed Patch generation. A separately maintained, **experimental local
headless sidecar** is under development for unattended leveled-list patching.
The sidecar is based on and modifies the **Wrye Bash v315** source and uses
the original Wrye Bash patching engine; it is **not an independently written
replacement** for that engine.

- Upstream project: [wrye-bash/wrye-bash](https://github.com/wrye-bash/wrye-bash)
- Upstream version: [v315](https://github.com/wrye-bash/wrye-bash/tree/v315)
  (commit `9b3e1c57a28f60580a6f15a7c0f5650a91781c89`)
- Original copyright notice: Copyright (C) 2005–2009 Wrye;
  2010–2026 Wrye Bash Team
- Upstream license: **GNU General Public License, version 3 or later**
  (GPL-3.0-or-later); see [Wrye Bash LICENSE.md](https://github.com/wrye-bash/wrye-bash/blob/v315/LICENSE.md)
  and source-file headers
- Scope of the modification: headless initialization, a leveled-lists-only
  build adapter, and a local process interface for the Sky-Claw/MO2 integration

**Licensing boundary.** Sky-Claw's root MIT license must not be represented
as relicensing Wrye Bash code or this derivative sidecar. The sidecar is
maintained as a distinct GPL-licensed component. This architectural separation
does not by itself settle every question about distribution of a combined work;
review the actual package and integration before publishing a release.

**Distribution gate (not satisfied by this notice alone).** Before shipping
any executable containing the modified Wrye Bash code, confirm that the exact
distributed binary is accompanied by, or supplied with a GPL-compliant means
to obtain, its **complete corresponding source** (including modifications and
necessary build/install scripts), the applicable full GPL license text,
original copyright notices, prominent modification notices, relevant
third-party dependency notices, and traceable source revision/build
information. Publish the actual source location and artifact identity for
each distributed version. An upstream source link alone is not a substitute
for the corresponding modified source.

The headless integration remains experimental and is not authorized for
production by this attribution notice. This independent integration is not
officially affiliated with, maintained by, or endorsed by the Wrye Bash Team.
