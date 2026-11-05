"""Download DeepDRiD in small batches, so a dropped connection loses very little."""
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("data/raw/DeepDRiD")
ROOT = "regular_fundus_images"
SPLITS = ["regular-fundus-validation", "Online-Challenge1&2-Evaluation", "regular-fundus-training"]
BATCH = 20  # patients per batch
MAX_BATCHES = int(sys.argv[1]) if len(sys.argv) > 1 else None
LOCKS = [REPO / ".git" / "index.lock", REPO / ".git" / "info" / "sparse-checkout.lock"]


def git(*args, stdin=None):
    return subprocess.run(["git", "-C", str(REPO), *args], input=stdin, text=True, capture_output=True)


def clear_locks():
    for lock in LOCKS:
        if lock.exists():
            lock.unlink()


def patient_folders():
    folders = []
    for split in SPLITS:
        out = git("ls-tree", "-d", "--name-only", "HEAD", f"{ROOT}/{split}/Images/")
        if out.returncode:
            sys.exit(out.stderr)
        folders += out.stdout.splitlines()
    return folders


def downloaded(folder):
    return (REPO / folder).exists() and any((REPO / folder).glob("*.jpg"))


folders = patient_folders()
done = [f for f in folders if downloaded(f)]
todo = [f for f in folders if f not in done]
print(f"{len(folders)} patient folders: {len(done)} already here, {len(todo)} to download", flush=True)

batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
for number, batch in enumerate(batches[:MAX_BATCHES], 1):
    wanted = done + batch
    for attempt in range(1, 7):
        clear_locks()
        result = git("sparse-checkout", "set", "--cone", "--stdin", stdin="\n".join(wanted) + "\n")
        if result.returncode == 0:
            done = wanted
            print(f"Batch {number}/{len(batches)} done: {len(done)}/{len(folders)} patients", flush=True)
            break
        wait = 15 * attempt
        reason = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
        print(f"Problem on batch {number}, attempt {attempt}: {reason}", flush=True)
        print(f"Trying again in {wait} seconds.", flush=True)
        time.sleep(wait)
    else:
        sys.exit("Stopped. Check the message above, then run this script again. It will continue from here.")

if MAX_BATCHES is None:
    print("All images downloaded. Now run: python scripts/check_data.py")