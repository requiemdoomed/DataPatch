# Security policy

## The hosted demo (GitHub Pages)

On the static site, all processing happens in your browser with Pyodide. Files you open are read
by the page and never uploaded anywhere. The page downloads Pyodide and pandas from
`cdn.jsdelivr.net`, pinned to a specific version. The `?pyodide=` option for self-hosting only
accepts paths on the same site, so a crafted link can't make the page load code from elsewhere.

## The local server

DataPatch's server is a local tool. `python app.py` listens on `127.0.0.1` only, keeps uploaded data in
memory and has no user accounts or authentication. Anyone who can reach the server can open any
session whose ID they know, so don't expose it on a public network or put sensitive data on a
shared server without adding authentication in front of it.

Debug mode is off unless you set `FLASK_DEBUG=1`. Never enable it on a machine others can reach.

When you open an exported CSV in a spreadsheet program, cells starting with `=`, `+`, `-` or `@`
can be treated as formulas. DataPatch exports values exactly as they are, so review files from
untrusted sources before opening them in a spreadsheet.

## Supported versions

Security fixes go into the latest release.

## Reporting a vulnerability

Please don't open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository ("Security" tab, then "Report a vulnerability"), with steps to reproduce.
You should get a reply within a week.
