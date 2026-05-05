# Installed Hermes Missions Plugin

The `missions` plugin has been installed and can be enabled with:

```bash
hermes plugins enable missions
```

If installed with `--enable`, restart any long-running Hermes gateway process before using it there:

```bash
hermes gateway restart
```

CLI usage:

```bash
hermes mission --help
hermes mission doctor
hermes mission create "Ship feature" --repo /path/to/repo
```
