# Network tool name ideas (from the music library)

Mined from the 885 tracks in `data/library.db`. Each name is a song title, or a close variation of one, that fits a network or OSINT tool. The song and artist are listed so you can trace it.

Name collisions were **not** checked. Search PyPI, npm, crates.io and GitHub before you commit to one.

## Top picks

| Name | Song (artist) | Why it fits |
|---|---|---|
| **apophenia** | アポフェニア (黒うさぎ / 重音テト) | Seeing patterns in noise. Ideal for a correlation or OSINT graph tool. |
| **lagtrain** | ラグトレイン / LAGTRAIN (稲葉曇) | Lag and latency. A ping, traceroute or latency monitor. |
| **rabbithole** | ラビットホールt (DECO*27) | Tunneling, pivoting, following a lead down the chain. |
| **panopticon** | Panopticon (r-906) | Sees everything on the network. Dashboard or monitor. Fairly common name. |
| **kokoniiru** | ここにいる (I'm Here) (Aiobahn) | A ping or host-discovery "I'm here" reply. Short alias: `koko`. |
| **netsuijo** | 熱異常 / Heat Abnormal (いよわ, むﾄ) | "Abnormal heat" suits anomaly or intrusion detection. Hard to guess, so likely unique. |
| **phony** | フォニイ / Phony (KAF, 星街すいせい) | Spoofing, decoys, honeypots. |
| **voidroid** | ボイドロイド / Voidroid (r-906) | A scanner that probes empty address space. |
| **jigen** | 次元通信 (ABM / 初音ミク / 重音テト) | "Dimensional communication". Short, CLI-friendly, good for a tunnel or relay. |
| **kaidenwa** | 怪電話 (r-906) | "Strange phone call". Unexpected inbound connections, or a VoIP/SIP recon tool. |

## By use case

### Recon, OSINT and discovery
- **apophenia**: AI-style correlation of scattered data points. (アポフェニア)
- **suiri** (推理, "deduction"): ATLUS, the Persona "Reasoning" track. A tool that infers topology from scraps.
- **reasoning**: the English title of the same ATLUS track.
- **suspicion** and **suspicious-person**: ATLUS. Flags odd hosts.
- **monitari** (モニタリング / Monitoring): DECO*27. Passive monitoring.
- **nite-patrol**: THEUKIYO tribute mix. Scheduled sweeps.
- **orrery**: Cicada Sirens, track 21. A model of orbiting bodies, so a map of hosts and their relations.
- **anemoia**: nostalgia for a time you never knew. A historical or archive lookup tool, such as the Wayback Machine.
- **panopticon**: see above.
- **eyelock**: ariiol. Targeted tracking of a single host.
- **debriefing**: Sheeno Mirin / Harusaruhi. Report generation after a scan.
- **OSINT**: Sheeno Mirin. This is literally a song title. Too generic to use as a project name.

### Packets, traffic and protocols
- **crosstalk**: Moe Shop. Interference between channels. A packet or traffic analyzer.
- **static**: vally.exe. Noise capture and sniffing.
- **lagtrain**: see above.
- **www**: Moe Shop. A web-focused crawler or fetcher. Too short to be unique, so add a suffix.
- **hoso** (放送✴︎, "broadcast"): 原口沙輔 / 重音テト. Broadcast and multicast tooling.
- **telepashi** (テレパシ / Telepathy): DECO*27. Host-to-host messaging.
- **typing**: ariiol / 歌愛ユキ. A banner-grab or interactive shell.
- **rhythm-of-writhing-constellations**: Bungie. Too long. `writhing` or `constellations` could work for mesh networking.
- **shuuden** (終電, "last train"): Mori Calliope. The last packet out, or a cron-style job.

### Tunnels, proxies and anonymity
- **rabbithole**: see above.
- **shadow-loop**: PSC. Loopback or reverse tunnel.
- **self-disguise**: PSC. Obfuscation or a proxy.
- **beneath-the-mask**: ATLUS. Masking or anonymizing. Shorten to `mask` or `beneath`.
- **ghost-avenue** (ゴーストアベニュー): Eve. A stealth route.
- **blind-alley**: ATLUS. A dead-end honeypot.
- **alleycat**: ATLUS. A scrappy, small tunnel utility.
- **phony**: see above.

### Anomaly detection and security
- **netsuijo**: see above.
- **parasite**: r-906. Detects hosts leeching bandwidth.
- **corruption**: 迪咲なのは cover. Integrity checking.
- **remains-of-a-corrupted-file**: s777n. Forensics or recovery. Use `corrupted` for the name.
- **disconnected**: ATLUS. Uptime and outage watcher.
- **lose-ctrl**: PBH / Sash Sings. Rogue-device detection.

### Odd and memorable (short names)
- **kumomi**: Nujabes. Soft sounding, tiny CLI.
- **saturator**: Purukichi (セトナツメ Saturator). Load testing or flooding. Use only on your own infrastructure.
- **splinter**: FIG. Splits traffic into streams.
- **mimic** (from クローンクローン, "Clone Clone"): Atena. Replay or cloning of requests.
- **ephemera**: Aethoro. Short-lived tokens and sessions.
- **iwatodai**: ATLUS, the dorm where the Persona 3 crew lives. A home-base daemon, a bastion or a gateway.
- **tartarus**: ATLUS. Deep tower, many floors. A scanner that goes layer by layer.

## Probably taken or too generic
nebula (Slack's overlay network), polaris, proxima, pulse, identity, notice, panopticon (many small projects), static.

## Shortlist
1. **apophenia** for OSINT or correlation
2. **lagtrain** for latency
3. **rabbithole** for tunnels
4. **netsuijo** for anomaly detection
5. **jigen** for a short CLI name
