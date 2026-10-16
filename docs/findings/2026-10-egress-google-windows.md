# 出口环境调研：Google `/sorry`、Linux vs Windows、sought 在 Windows 上的表现

日期：2026-10-16。所有实验每项只做一次（Google 风控是概率性的，单次结果是一个样本，不是定律）。
代理 URL 中的凭据、token 一律未记录；内网主机名、内网 IP、出口 IP、账号与公司安全产品名称已脱敏（`<corp-proxy>`、`<egress-IP>` 等占位符）。

## 1. 背景与问题

同一公司网络下，Linux 开发机经 cntlm（`localhost:3128`）→ 上游 `<corp-proxy>:3128`
出网，出口 IP `<egress-IP>`。从该 IP 自动化访问 Google 搜索第一次就被跳转到 `/sorry/index`。
用户在 Windows 浏览器里用 Google 正常。

要回答：两边差异是 **出口 IP 不同**，还是 **cookie/登录状态不同**，还是别的。

## 2. 网络环境（Windows 主机）

| 项 | 结果 |
|---|---|
| 位置 | 办公室有线内网（`10.x.x.0/24`，网络配置文件 `DomainAuthenticated`），无 VPN |
| 安全客户端 | 无 Zscaler / Netskope / GlobalProtect / Cisco / Forcepoint / cntlm / px；仅有终端管理代理 |
| WinINET | `ProxyEnable=0`，`AutoConfigURL=http://<corp-pac-host>/wpad.dat`，`AutoDetect=0` |
| WinHTTP | Direct |
| 环境变量 | 仅 `NO_PROXY=localhost,127.0.0.1,<corp-domain>`，无 `HTTP(S)_PROXY` |
| 浏览器策略 | Edge / Chrome：`ProxyMode=system`；Edge `QuicAllowed=0` |
| PAC 结果 | `www.google.com`、`search.brave.com`、`www.startpage.com`、`ifconfig.me` → `PROXY <corp-proxy>:3128` |
| DNS | Linux cntlm 的上游主机名经 CNAME 解析到与 PAC 所指相同的主机（`10.x.x.x`），即 PAC 与 Linux cntlm 上游是**同一台代理** |
| Kerberos | UPN `<user>@<corp-domain>`，有 TGT；无 代理的 `HTTP/` 服务票据 |
| 直连 | 不经代理也能出网：流量被**透明拦截**，出口同为 `<egress-IP>`（HTTP 与 HTTPS 均如此）。`curl.exe` 直连 HTTPS 报 exit 35，原因是 schannel `CRYPT_E_NO_REVOCATION_CHECK`（直连时查不到 SASE 证书的吊销信息），加 `--ssl-no-revoke` 即成功；Python `requests`+`truststore` 直连成功。部分站点（如 pypi.org / Fastly）直连时未被解密 |
| TLS 解密 | 代理对多数 HTTPS 站点做 TLS inspection，签发者为公司 SASE 网关的私有 CA（该 CA 在 Windows 信任库中）；**`www.google.com` 不解密**（签发者 Google Trust Services） |

出口 IP：浏览器、`Invoke-WebRequest`（系统代理/PAC）、直连 HTTP、WSL 经 cntlm —— 全部为 **`<egress-IP>`**
（AS21859 Zenlayer，新加坡，hosting=true；ipinfo.io 对该 IP 返回 429，说明被大量共用）。

## 3. Google 对照实验

所有组：出口 `<egress-IP>`，全新 profile，零 cookie，未登录 Google，搜索 `python asyncio taskgroup` 一次。

| # | 平台 | 启动方式 | 有界面 | UA | `navigator.webdriver` | Google |
|---|---|---|---|---|---|---|
| A | Windows | Edge 155 手动启动（临时 profile）+ Playwright `connectOverCDP` | 是 | `Chrome/155 … Edg/155` | false | ✅ 8 条结果 |
| B | WSL Linux | Chromium 147 手动启动 `--proxy-server=cntlm` + `connectOverCDP` | 是 | `Chrome/147`（X11） | false | ✅ 9 条结果 |
| C | WSL Linux | `chromium.launch(headless=new)` | 否 | `HeadlessChrome/147` | true | ❌ `/sorry` |
| D | WSL Linux | `chromium.launch` headless_shell | 否 | `HeadlessChrome/147` | true | ❌ `/sorry` |
| E | WSL Linux | `chromium.launch(headless=False)` + `new_context()` | 是 | `Chrome/147` | true | ❌ `/sorry` |
| F | WSL Linux | 同 E + `--disable-blink-features=AutomationControlled` | 是 | `Chrome/147` | **false** | ❌ `/sorry` |
| G | WSL Linux | 同 B，紧接 E/F 之后复测 | 是 | `Chrome/147` | false | ✅ 8 条结果 |
| H | WSL Linux | `curl` + Chrome UA | — | Chrome UA | — | ⚠️ HTTP 200 但只有 "enable JavaScript" 页，无结果 |

附带：Brave 在真实浏览器里也对该 IP 返回 429 + "Verifying you're not a bot"（按 IP 判定）；
Startpage 出 JS 工作量证明（"Verifying your request… Difficulty 6"），真实浏览器 ~10–20 s 后自动通过。

### 结论

1. **出口 IP 相同**：Linux 与 Windows 都是 `<egress-IP>`，经同一台公司代理。
2. **不是 cookie/登录**：A、B、G 零 cookie 未登录也放行。
3. **不是 IP 被封**：G 在 E/F 被拦之后立即放行 —— 判定是**逐请求按客户端特征**做的。
4. **不是 UA**：E/F 的 UA 与 B/G 相同。
5. **主因是 `navigator.webdriver=true`**（§4 逐项排查确认）。F 看似反例，但它同时用了 `new_context()`
   + 默认 viewport，这是 §4 中发现的第二个、较弱的因素。
6. 不执行 JS 的 HTTP 客户端拿不到 Google 结果（H）。

> 对 sought 的含义：CLAUDE.md 中 "A shared NAT egress IP can be banned network-wide by Google" 的判断，
> 在本网络上**不成立** —— 同一 IP 上真实浏览器可用。sought 不直接查 Google 的决定本身无需改变
> （它走 Startpage 拿 Google 索引），但理由应改为"自动化浏览器（`navigator.webdriver`）被逐请求拦截"，
> 而不是"IP 被封"。

## 4. Playwright 启动参数逐项排查

环境：WSL Ubuntu-24.04，Chrome for Testing 147.0.7727.15，playwright-core 1.64.1，经 cntlm，出口 `<egress-IP>`。
12 次探测，间隔 35–45 s，每次一个全新 profile、一次导航。对照组 = §3 的 B（手动启动 + `connectOverCDP`，
用默认上下文）。4 次对照（#1/#5/#12，以及 #6）全部放行 → 期间 IP 没有被标记。被拦时一律 302 → `/sorry` → 429。
UA 在所有探测中相同（`X11; Linux x86_64 … Chrome/147.0.0.0`）。

Playwright 1.64 headed 默认开关（`chromiumSwitches` + `_innerDefaultArgs`）：`--disable-field-trial-config
--disable-background-networking --disable-component-update --disable-features=…(18 项) --disable-sync
--metrics-recording-only --no-sandbox …` 等约 40 项，外加 `--proxy-server … --proxy-bypass-list=<-loopback>`、
`--user-data-dir`、`--remote-debugging-pipe`。**1.64 已不再默认传 `--enable-automation`**，但所有 Playwright
启动的实例（包括 `ignoreDefaultArgs: true`）`navigator.webdriver` 仍为 true。

| # | 启动方式 / 改动 | 上下文 | webdriver | Google |
|---|---|---|---|---|
| 1 | 对照 | 默认 | false | ✅ 8 |
| 2 | `launchPersistentContext`，全部默认参数 + proxy 选项 | 持久化，viewport 1280×720 | true | ❌ |
| 3 | `launch`，`ignoreDefaultArgs: true`，最少参数，手写 `--proxy-server` | `newContext` | true | ❌ |
| 4 | 对照启动 + CDP | `browser.newContext()`（默认 1280×720 viewport） | false | ❌ |
| 5 | 对照 | 默认 | false | ✅ 8 |
| 6 | 对照 + `page.setViewportSize(1280×720)` | 默认 | false | ✅ 9 |
| 7 | 对照启动 + CDP | `newContext({viewport: null})` | false | ✅ 8 |
| 8 | `launchPersistentContext`，全部默认参数，`viewport: null` | 持久化 | true | ❌ |
| 9 | **对照 + 手动加 `--enable-automation`** | 默认 | **true** | ❌ |
| 10 | 同 #8 + `--disable-blink-features=AutomationControlled` | 持久化，viewport null | false | ✅ 8 |
| 11 | 标准 `launch`（全部默认参数 + proxy 选项）+ `AutomationControlled` 关闭 | `newContext({viewport: null})` | false | ✅ 8 |
| 12 | 对照 | 默认 | false | ✅ 8 |

### 结论

1. **`navigator.webdriver=true` 是决定因素（高置信）。** 5 次 webdriver=true 全部被拦（#2、#3、#8、#9 与 §3-E）。
   #9 是最干净的证据：对照组只加一个 `--enable-automation` 就被拦。#10、#11 保留 Playwright 全部默认开关、proxy
   选项、pipe 传输，只把 webdriver 关掉就放行 —— 因此**排除**：默认开关、`--proxy-bypass-list`、
   `--remote-debugging-pipe`、Playwright 运行时、持久化 vs 隐身上下文。
2. **`newContext()` + 默认 viewport 是第二个弱因素（低–中置信）。** webdriver=false 仍被拦的两例（#4、§3-F）
   都是带默认 viewport 的 `newContext()`；只改 `viewport: null` 就放行（#7 vs #4，#11 vs F）。但在默认上下文上
   设同样的 viewport 是放行的（#6）。每种只测了一次，可能是噪声；上下文级 emulation 具体改变了什么未验证。
3. 放行的组合（各 1 次，未验证持续/批量查询下是否成立）：`--disable-blink-features=AutomationControlled`
   + `viewport: null`，`launch()+newContext({viewport:null})` 或 `launchPersistentContext(…, {viewport:null})` 均可。

> 与 sought 的关系：sought 用 `headless=True` 并调用 `new_context(viewport=1280×900)`，按上表会被 Google 拦 ——
> 但 sought 本来就不查 Google。关闭 `AutomationControlled` 属于 CLAUDE.md 明确排除的 "fingerprint spoofing"，
> **不建议**为了 Google 把它加进 sought；这里的价值在于修正 "IP 被封" 这一错误归因。

## 5. sought 在 Windows 上的实测

HEAD `b6d92c4`，Windows 11，PowerShell 7，控制台代码页 936（GBK）。每条命令只跑一次。未设置任何代理环境变量。

| 步骤 | 退出码 | 耗时 | 结果 |
|---|---|---|---|
| `uv sync` | 0 | 31 s | Python 3.13.15，playwright **1.62.0**，sought 0.1.0 |
| `uv run playwright install chromium` | 0 | 40 s | 经 PAC 代理从 `cdn.playwright.dev` 下载成功，未被拦；需要 revision **1234**（Chrome for Testing 151），已有的 1194/1223 用不上 |
| `uv run sought doctor` | **1** | — | **崩溃**：`doctor.py:78 print(...)` → `UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'`。崩溃发生在网络探测之后，探测结果丢失；但已写入 `cooldown.json`：startpage `bot wall: 'verifying your request'`，marginalia `bot wall: 'queries from bots'` |
| `doctor --offline`（`PYTHONIOENCODING=utf-8`，仅对该进程） | 0 | 2 s | 全绿 + cooldown 警告；`proxy: none set — all engines connect directly`（误导，见 §6.4）；chromium = `%LOCALAPPDATA%\ms-playwright\chromium-1234\chrome-win64\chrome.exe` |
| `sought "python asyncio taskgroup" --pretty`（等 cooldown 过期后，UTF-8） | 0 | 23 s | `✗ brave: rate_limited — HTTP 429`，`✓ startpage: 10 results`（docs.python.org、Stack Overflow 等，相关；#8 是 `startpage.com/clev?…` 跳转链接而非直链） |
| 同一命令第二次 | 0 | 1 s | `(cached, 6s old — use --fresh to search again)`，attempts 与第一次相同 ✓ |
| `sought "requests" --source pypi --pretty` | 0 | 2 s | `✓ pypi: 1 results`：requests 2.34.2 |
| `uv run just check` | **1** | 11 s | ruff 通过；pytest **272 passed, 2 failed**（均为 Windows 特有，见下） |

缓存中的审计记录（`%USERPROFILE%\.cache\sought\results\*.json`）：
`[{"engine":"brave","status":"rate_limited","detail":"HTTP 429"},{"engine":"startpage","status":"ok","count":10}]`，
`low_relevance: false` —— 审计轨迹完整，符合 invariant。

观察：

- **没有证书错误**：Windows 上的 Chromium 使用 Windows 证书库，SASE CA 被信任 → brave/startpage 不会出现
  Linux 上的 `ERR_CERT_AUTHORITY_INVALID`。CLAUDE.md "Chromium does not read the OS trust store" 只对 Linux 成立。
- **Startpage**：`doctor` 探测时被 PoW 页判为 `rate_limited`（headless 下 15 s selector 超时内没过完 PoW），
  10 分钟后真实搜索成功。PoW 是否通过与时机/难度有关，doctor 的单次探测结果不稳定。
- **Brave**：与真实浏览器一样，按 IP 返回 429。
- **marginalia**：doctor 探测时返回 "queries from bots" 节流页（被正确识别为 bot wall，而非 `empty`）。

### Windows 特有的缺陷

1. **控制台编码崩溃（严重）。** `--pretty` 与 `doctor` 输出 `✓ ✗ — →` 等非 ASCII 字符，在 GBK（以及任何非 UTF-8
   代码页）控制台上 `print` 抛 `UnicodeEncodeError`，进程退出码 1 —— **与 "searched fine, found nothing" 的 1
   无法区分**，违反 invariant 中 "exit 1 与 2 是不同事实" 的要求；doctor 则在探测完成后丢掉全部结果。
   默认 JSON 输出（`ensure_ascii=False`）遇到非 ASCII 结果标题时大概率同样崩溃（未实测）。
   修复方向：入口处 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`，或在不能编码时降级为 ASCII 符号。
2. **测试假设环境变量区分大小写。** Windows 环境变量不区分大小写：
   - `tests/test_doctor.py::test_credentials_in_an_environment_proxy_are_not_printed` —— `delenv("https_proxy")`
     同时删掉了刚设的 `HTTPS_PROXY`；
   - `tests/test_net.py::test_the_lowercase_variable_wins_over_the_uppercase_one` —— 设 `NO_PROXY` 覆盖了 `no_proxy`。
   "大小写冲突" 场景在 Windows 上不存在，这两个测试应在 Windows 上 skip（或改写成平台无关的断言）。

### Windows 上 sought 的文件位置

- 配置：`C:\Users\<user>\.config\sought\config.toml`（未创建）
- 状态/缓存：`C:\Users\<user>\.cache\sought\`（`cooldown.json`、`results\*.json`）
- Playwright 浏览器：`%LOCALAPPDATA%\ms-playwright\chromium-1234\`、`chromium_headless_shell-1234\`（约 300 MiB）

## 6. sought 源码分析：Windows 上的行为差异

### 6.1 `net.session()` / `net.environment_proxy()` —— requests 与 Chromium 走不同路径

本机实测（项目 venv 的 Python 3.13）：

```
urllib.request.getproxies_environment() → {'no': 'localhost,127.0.0.1,<corp-domain>'}
urllib.request.getproxies()             → {'no': 'localhost,127.0.0.1,<corp-domain>'}
urllib.request.getproxies_registry()    → {}
```

- `getproxies()` 在 Windows 上 = 环境变量，若为空再读注册表 `ProxyServer`，但**只在 `ProxyEnable=1` 时**；
  它**不解析 PAC（`AutoConfigURL`）也不做 WPAD**。本机 `ProxyEnable=0` + PAC，所以结果为空。
- `requests.Session()`（`trust_env=True`）用的正是 `getproxies()` → **没有代理 → 直连**。
- `environment_proxy()` 用 `getproxies_environment()` → `None` → `_launch_kwargs()` 不给 Chromium 传代理。
- Chromium 在 Windows 上没有 `--proxy-server` 时使用**系统代理设置（含 PAC）** → 走公司代理。

即：在 Windows + PAC 网络上，**两条传输路径不一致**，方向与 CLAUDE.md 记录的 GNOME 案例相反
（那里是 requests 有代理、Chromium 直连；这里是 Chromium 走 PAC 代理、requests 直连）。
`environment_proxy()` 的 docstring 所述的保证（"The two transports must not disagree about the route"）在此不成立。

**本网络上碰巧无害**：直连流量被透明拦截，从同一出口 `<egress-IP>` 出去，`truststore` 信任 SASE CA，
所以 pypi / marginalia 实测都能连通（§5）。在"直连被防火墙拒绝、只能走 PAC 代理"的网络上（例如 CLAUDE.md
提到的 Kerberos 401 防火墙），这会表现为：browser 引擎正常，`marginalia` 与所有 vertical source 失败 ——
而 `doctor` 会说 "none set — all engines connect directly"，把读者引向错误方向。

另外，即使 `ProxyEnable=1`，`getproxies_registry()` 能读到 `ProxyServer`，`environment_proxy()` 仍看不到它
（只看环境变量），Chromium 也只是碰巧因为自己读系统设置才一致。

### 6.2 `browser._launch_kwargs()` / `_acquire()`

- 没有显式代理时 Chromium 用系统代理/PAC，与 requests 不一致（见 6.1）。
- `headless=True` 固定：结合 §3，Google 会拦 headless；Startpage/Brave 对 headless 的态度另见 §5 实测。
- `new_context(user_agent=DEFAULT_UA)` 把 UA 固定为 `X11; Linux x86_64 … Chrome/120.0`：在 Windows 上，
  UA 声称 Linux / Chrome 120，而 `Sec-CH-UA*` 客户端提示与 JS 环境报告的是 Windows / 实际版本（14x）。
  这种不一致本身就是常见的 bot 信号；Linux 上版本号也对不上。
- `--no-sandbox` 在 Windows 上无必要（它是为 Linux 容器/root 场景准备的）。

### 6.3 `state.cache_dir()` 与 `config.CONFIG_DIR`

- 缓存：`Path.home() / ".cache" / "sought"` → `C:\Users\<user>\.cache\sought\`。
- 配置：`Path.home() / ".config" / "sought" / "config.toml"` → `C:\Users\<user>\.config\sought\config.toml`。
- 能用，但不符合 Windows 惯例（应为 `%LOCALAPPDATA%\sought\Cache` 与 `%APPDATA%\sought`）。
  可接受的折中：保留 XDG 变量优先，Windows 上默认落到 `%LOCALAPPDATA%` / `%APPDATA%`。
  （`config.py` 里 `XDG_CONFIG_HOME` 未做 "相对路径无效" 的校验，与 `state.py` 不一致 —— 平台无关的小问题。）

### 6.4 `doctor` 在 Windows 上的误导

- `_tls_note()`：证书失败时只给 Linux 方案（`certutil -d sql:$HOME/.pki/nssdb …`、`libnss3-tools`）。
  Windows 上 `certutil` 是一个**同名但完全不同**的系统工具，照抄会报错或做别的事；且 Windows 上 Chromium
  会信任本机 Windows 证书库里用户/企业添加的根证书，NSS 数据库不存在。前提 "Chromium reads its own NSS
  database, not the OS trust store" 在 Windows 上不成立 —— §5 实测 Chrome for Testing 151 在 Windows 上
  信任 SASE CA，没有证书错误。
- `_proxy_checks()`：只看 4 个环境变量，Windows 上报告 "none set — all engines connect directly"，
  而实际上 Chromium 走 PAC 代理、requests 直连 —— **两条路径都和这句话描述的不一样**（前者不直连，后者直连但
  失败）。应识别 WinINET/PAC 并明确说明 requests 不会使用 PAC。
- README 的 TLS 一节同样只写了 Linux 路径。

## 7. 建议（未实施）

1. **输出编码**（§5 缺陷 1，优先级最高）：非 UTF-8 控制台上不能崩溃，更不能以退出码 1 崩溃。
2. **测试**（§5 缺陷 2）：大小写冲突类测试在 Windows 上 skip；并在 CI 中加 Windows runner。
3. `net`：Windows 上若环境变量为空，检测 `AutoConfigURL` / `ProxyEnable` 并在 `doctor` 中明确提示
   "requests 不会使用 PAC，请在 `[proxy]` 中显式配置"；或者引入 PAC 解析（例如 `pypac`）——后者是依赖与行为变更，需要权衡。
4. `doctor`：TLS 提示按平台分支；代理检查报告 WinINET/PAC 状态与"两条传输是否一致"。
5. `state`/`config`：Windows 上默认使用 `%LOCALAPPDATA%` / `%APPDATA%`。
6. `browser`：去掉固定的 Linux/Chrome 120 UA（或至少与实际平台/版本一致）；Windows 上去掉 `--no-sandbox`。
7. CLAUDE.md 中关于 Google "IP 被封" 的表述按 §3 修正。
