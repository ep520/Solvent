# CallGuard on the organizer's SSH pod (A40, builder-vlm kit)

The pod is reached only by SSH; there is no web port. Everything below runs in **Git Bash** on Windows
(installed with Git), from the folder that contains the kit's `connection.sh` and `access/`.
Never copy `access/` into the repo or onto the pod: it is a root key, and the repo is public.

## 1. Connect once (Git Bash, in the kit folder)
```
chmod 700 access; chmod 600 access/id_ed25519 access/known_hosts
source connection.sh
SSHOPT=(-i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$SSH_KNOWN_HOSTS")
ssh "${SSHOPT[@]}" -p "$SSH_PORT" root@"$POD_IP" nvidia-smi
```

## 2. Upload code and data
```
scp "${SSHOPT[@]}" -P "$SSH_PORT" -r /c/path/to/Solvent root@"$POD_IP":/workspace/
scp "${SSHOPT[@]}" -P "$SSH_PORT" -r /c/path/to/data   root@"$POD_IP":/workspace/
```
(`/c/path/to/...` is how Git Bash writes `C:\path\to\...`. `data` holds `Audio/`, `Transkript/`,
`Skript_mit_Sollbewertung/`, `Stichwortliste.json`.)

## 3. On the pod: install, start Qwen3, prepare the calls
```
ssh "${SSHOPT[@]}" -p "$SSH_PORT" root@"$POD_IP"
apt-get update && apt-get install -y tmux
tmux new -s callguard
cd /workspace/Solvent
bash runpod/setup.sh                              # first time 15-25 min: venvs + model download
bash runpod/run_dashboard.sh --only D02,C05       # two calls first; ends by serving the dashboard
```
Detach from tmux with **Ctrl+B, then D**; the work keeps running. Re-attach: `tmux attach -t callguard`.
When the two calls look right, stop the dashboard (Ctrl+C) and run `bash runpod/run_dashboard.sh` for all calls.

## 4. See the dashboard on your PC
In a second Git Bash window (kit folder):
```
source connection.sh
ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$SSH_KNOWN_HOSTS" \
    -p "$SSH_PORT" -N -L 8090:127.0.0.1:8090 root@"$POD_IP"
```
Leave it open and browse **http://127.0.0.1:8090**. Audio plays through the same tunnel.

## 5. Evaluate and save
```
bash runpod/run_eval.sh          # metrics vs Skript_mit_Sollbewertung -> runs/eval-*/summary.json
```
Copy results to your PC (Git Bash, kit folder):
```
scp "${SSHOPT[@]}" -P "$SSH_PORT" root@"$POD_IP":/workspace/dashboard_results.zip .
scp "${SSHOPT[@]}" -P "$SSH_PORT" -r root@"$POD_IP":/workspace/Solvent/runs .
```

## Hidden test (new recordings, frozen configuration)
Keep new recordings out of `data/Audio`: the full development evaluation requires gold for every file there.
```
mkdir -p data/Hidden                                   # upload the new WAVs here
python3 -m callguard.pipeline predict data/Hidden --workers 4
```
`predict` reads no labels, refuses `--threshold` (Balanced 0.60 is frozen; all presets are recorded), refuses
non-self-hostable profiles and human transcript corrections for these recordings, and seals its outputs
(`SEALED.json`, fingerprint printed). Download `runs/predict-*` before labels exist. When labels arrive:
```
python3 -m callguard.pipeline score runs/predict-YYYYMMDD-HHMMSS-Hidden --labels data/Hidden_labels
```
`score` verifies the seal, then writes `score-*/summary.json` (standard metrics) next to the untouched predictions.

## Stop billing
The pod costs about $0.60/hour and has **no automatic shutdown**. Closing SSH does not stop it.
Stop it in the RunPod console (pod `hackathon-202610-builder-vlm`). `/workspace` keeps venvs, models and results.

## If something fails
| Symptom | Fix |
|---|---|
| `vLLM stopped` | `tail -50 /workspace/vllm.log`; out of memory → `MODEL=Qwen/Qwen3-14B-AWQ bash runpod/setup.sh` |
| `libcublas`/`libcudnn` not found | `source runpod/env.sh` in that terminal |
| `metadata_errors` from av | `/workspace/callguard-env/asr/bin/pip install av==16.0.1` |
| JSON schema / response_format error | set `"json_mode": "json_object"` for the `qwen3` profile in `config/models.json` |
| Calls very slow | `CALLGUARD_CHAT=qwen3_fast bash runpod/run_dashboard.sh` (thinking off) |
| Dashboard shows all calls as pending | the dashboard must run with the same `QWEN_MODEL` as `prepare` (`source runpod/env.sh`) |
