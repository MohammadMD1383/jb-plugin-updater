# jb-plugin-updater

CLI tool that automatically updates plugins installed in JetBrains IDEs. Discovers your IDE installed via JetBrains Toolbox, reads installed user plugins, queries the JetBrains Marketplace for compatible updates, and installs them — all with a live TUI.

## Requirements

- Python 3.10+
- JetBrains Toolbox installed IDE (primary), or manually specified `--plugins-dir`

## Installation

```bash
pip install requests rich
```

## Usage

```bash
python update-plugins.py <ide>              # check and install updates
python update-plugins.py <ide> --dry-run    # check only, do not install
python update-plugins.py <ide> --list-only  # list installed plugins and exit
python update-plugins.py <ide> --plugins-dir /path/to/plugins  # override plugins dir
python update-plugins.py <ide> -x http://127.0.0.1:10808       # use a proxy
```

## IDE Aliases

| Alias | IDE |
|-------|-----|
| `cl` | CLion |
| `ij` / `iu` / `idea` / `intellij` / `idea-u` | IntelliJ IDEA Ultimate |
| `ic` / `idea-ce` | IntelliJ IDEA Community Edition |
| `py` / `pc` / `pycharm` | PyCharm Professional |
| `pce` / `pycharm-ce` | PyCharm Community Edition |
| `ws` / `webstorm` | WebStorm |
| `go` / `gl` / `goland` | GoLand |
| `rd` / `rider` | Rider |
| `ps` / `phpstorm` | PhpStorm |
| `rm` / `rubymine` | RubyMine |
| `dg` / `datagrip` | DataGrip |
| `rr` / `rustrover` | RustRover |
| `ds` / `dataspell` | DataSpell |
| `gw` / `gateway` | Gateway |
| `aqua` | Aqua |
| `fleet` | Fleet |
| `mps` | MPS |

## Platform Support

- **Linux** (primary)
- **macOS / Windows** (best-effort via Toolbox)
- Non-Toolbox installs: detected via common paths or `--plugins-dir` override

## Proxy Support

Set via CLI flag (`-x`) or standard environment variables:

- `https_proxy` / `HTTPS_PROXY`
- `http_proxy` / `HTTP_PROXY`
- `all_proxy` / `ALL_PROXY`

## License

MIT
