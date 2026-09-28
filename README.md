# papercast-group

Papers become podcast episodes for a research group: a narrated episode, an explainer page and
links in a shared paper graph. Everyone can browse and listen on the web; contributors add
papers from the command line, and Claude runs on their own machine under their own login.

## Parts
- `stacks/papercast-group/hub/`: the server and web page (library, per-person Listened and
  positions, the editable map with undo, admin), stdlib Python plus numpy for the map layout.
- `packages/papercast-cli/`: the `papercast` command (`login`, `add <pdf|url>`, `status`,
  `prefs`) and the local episode pipeline.
- `stacks/papercast-group/deploy/`: install on a Linux machine as a normal user with systemd
  user units (server, voice worker, nightly backup and layout); `README.md` there covers
  Cloudflare for public access.
- `stacks/papercast/voice/`: papercast-voice (Breeze TTS 2 on a GPU), used by the voice worker.
- `stacks/papercast-group/SPEC.md`: the contract between the parts;
  `stacks/papercast-group/prompts/`: the group's base prompt and how preferences layer on it.

## Contributors
```
pipx install 'git+<this repo>#subdirectory=packages/papercast-cli'
papercast login --server https://<the group's hub>
papercast add paper.pdf https://arxiv.org/abs/2006.11239
papercast status
```
Needs Claude Code installed and logged in (`claude`), Python 3.10 or newer; poppler
(`pdftoppm`) is optional, for figure crops. macOS and Linux; Windows through WSL.

## Tests
```
cd packages/papercast-cli && python3 -m unittest discover -s tests
cd stacks/papercast-group/hub && python3 -m unittest discover -s tests -t ..
python3 -m unittest discover -s stacks/papercast-group/tests -p 'e2e_test.py'
```
