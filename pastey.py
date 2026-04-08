#!/usr/bin/env python3
"""
Pastey_v1.0 — AI Terminal Bridge
Paste AI response at the > prompt.
Pastey extracts code blocks, runs them, copies result back.
"""

import subprocess
import time
import sys
import re
import os
import signal
import select
import threading
from datetime import datetime

HANG_TIMEOUT  = 120
MAX_RETRIES   = 3

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    DIM    = "\033[2m"

def log(msg, colour=C.CYAN):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{C.DIM}[{ts}]{C.RESET} {colour}{msg}{C.RESET}")

def warn(msg):  log(f"⚠  {msg}", C.YELLOW)
def err(msg):   log(f"✗  {msg}", C.RED)
def ok(msg):    log(f"✓  {msg}", C.GREEN)
def info(msg):  log(f"   {msg}", C.DIM)

def set_clipboard(text):
    try:
        proc = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE)
        proc.communicate(input=text.encode())
    except Exception:
        err("xclip not found — install: sudo apt install xclip")

def extract_code_block(text):
    patterns = [
        r"```(?:bash|sh|shell|zsh)\n(.*?)```",
        r"```\n(.*?)```",
    ]
    matches = []
    for pattern in patterns:
        found = re.findall(pattern, text, re.DOTALL)
        matches.extend(found)
    if not matches:
        return None
    code = matches[-1].strip()
    dangerous = ["rm -rf /", "mkfs", "dd if=", "> /dev/sd", ":(){ :|:& };:"]
    for danger in dangerous:
        if danger in code:
            err(f"DANGER: Refusing to run: {danger}")
            return None
    return code

def run_command_captured(command):
    script_path = "/tmp/pastey_cmd.sh"
    output_file = "/tmp/pastey_output.txt"

    # Detect Python vs bash
    first = command.strip().split("\n")[0]
    is_python = any(first.startswith(x) for x in [
        "import ", "from ", "def ", "class ", "print(", "for ", "while ",
        "if ", "try:", "with ", "#!"
    ])

    if is_python:
        script = "python3 /tmp/pastey_run.py"
        with open("/tmp/pastey_run.py", "w") as pf:
            pf.write(command)
        wrapped = script + " 2>&1 | tee " + output_file + "\necho $? > /tmp/pastey_exit.txt\n"
    else:
        wrapped = "(\n" + command + "\n) 2>&1 | tee " + output_file + "\necho $? > /tmp/pastey_exit.txt\n"

    with open(script_path, "w") as f:
        f.write(wrapped)
    os.chmod(script_path, 0o755)

    start = time.time()
    try:
        proc = subprocess.Popen(
            ["bash", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        output_lines = []
        sudo_detected = False

        def read_output():
            nonlocal sudo_detected
            for line in proc.stdout:
                output_lines.append(line)
                if re.search(r'\[sudo\] password|Password:', line, re.I):
                    sudo_detected = True
                sys.stdout.write(f"  {C.DIM}{line}{C.RESET}")
                sys.stdout.flush()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        try:
            proc.wait(timeout=HANG_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGINT)
            time.sleep(1)
            proc.kill()
            return {"output": "".join(output_lines), "exit_code": -1,
                    "status": "timeout", "duration": time.time() - start, "command": command}

        reader.join(timeout=2)
        duration = time.time() - start

        if sudo_detected:
            return {"output": "".join(output_lines), "exit_code": proc.returncode,
                    "status": "sudo", "duration": duration, "command": command}

        status = "ok" if proc.returncode == 0 else "error"
        return {"output": "".join(output_lines), "exit_code": proc.returncode,
                "status": status, "duration": duration, "command": command}

    except Exception as e:
        return {"output": str(e), "exit_code": -1, "status": "crash",
                "duration": time.time() - start, "command": command}

def format_result_for_ai(result):
    lines = [
        "```",
        "$ " + result["command"],
        result["output"].strip() if result["output"].strip() else "(no output)",
        "```",
        "Exit code: " + str(result["exit_code"]) + " | Time: " + f"{result['duration']:.1f}s"
    ]
    if result["status"] == "error":
        lines.append("Command failed with exit code " + str(result["exit_code"]) + ". Please fix.")
    elif result["status"] == "timeout":
        lines.append("Command timed out after " + str(HANG_TIMEOUT) + "s and was killed.")
    elif result["status"] == "sudo":
        lines.append("Command requires sudo password.")
    elif result["status"] == "crash":
        lines.append("Command crashed: " + result["output"])
    return "\n".join(lines)

class Pastey:
    def __init__(self):
        self.running       = True
        self.retry_count   = 0
        self.last_command  = ""
        self.session_stats = {"run": 0, "ok": 0, "error": 0, "skipped": 0}

    def check_dependencies(self):
        missing = []
        for tool in ["xclip", "xdotool", "wmctrl"]:
            r = subprocess.run(["which", tool], capture_output=True)
            if r.returncode != 0:
                missing.append(tool)
        if missing:
            err("Missing tools: " + ", ".join(missing))
            err("Install: sudo apt install " + " ".join(missing))
            return False
        return True

    def confirm_run(self, code):
        print()
        print(f"{C.CYAN}{'='*60}{C.RESET}")
        print(f"{C.BOLD}CODE BLOCK DETECTED:{C.RESET}")
        print(f"{C.CYAN}{'-'*60}{C.RESET}")
        for i, line in enumerate(code.splitlines(), 1):
            print(f"  {C.DIM}{i:3}{C.RESET}  {line}")
        print(f"{C.CYAN}{'-'*60}{C.RESET}")
        print(f"  {C.GREEN}[Enter]{C.RESET} Run   {C.YELLOW}[s]{C.RESET} Skip   {C.RED}[q]{C.RESET} Quit")
        print(f"{C.CYAN}{'='*60}{C.RESET}")
        try:
            choice = input(f"{C.BOLD}> {C.RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            choice = "q"
        if choice == "q":
            self.running = False
            return False
        if choice == "s":
            self.session_stats["skipped"] += 1
            info("Skipped.")
            return False
        return True

    def handle_result(self, result):
        self.session_stats["run"] += 1
        status = result["status"]
        if status == "ok":
            ok("Done in " + f"{result['duration']:.1f}s")
            self.session_stats["ok"] += 1
            self.retry_count = 0
        elif status == "error":
            self.session_stats["error"] += 1
            self.retry_count += 1
            warn("Failed (attempt " + str(self.retry_count) + "/" + str(MAX_RETRIES) + ")")
            if self.retry_count >= MAX_RETRIES:
                err("Max retries reached — stopping.")
                self.running = False
        elif status == "timeout":
            warn("Timed out — killed.")
        elif status == "sudo":
            warn("sudo needed — enter password in terminal then press Enter here.")
            input()
        elif status == "crash":
            err("Crashed!")
            self.running = False
        return format_result_for_ai(result)

    def print_banner(self):
        cyan = C.CYAN
        reset = C.RESET
        dim = C.DIM
        print()
        print(cyan + "┏━┓┏━┓┏━┓╺┳╸┏━╸╻ ╻" + reset)
        print(cyan + "┣━┛┣━┫┗━┓ ┃ ┣╸ ┗┳┛" + reset)
        print(cyan + "╹  ╹ ╹┗━┛ ╹ ┗━╸ ╹ " + reset + "  " + dim + "v1.0 — AI Terminal Bridge" + reset)
        print()
        print(dim + "  Paste AI response at > prompt" + reset)
        print(dim + "  Pastey extracts code → runs it → copies result back" + reset)
        print(dim + "  [Enter] run  |  [s] skip  |  [q] quit  |  Ctrl+C stop" + reset)
        print()

    def print_stats(self):
        s = self.session_stats
        print()
        print(f"{C.DIM}Session: {s['run']} run | {s['ok']} ok | {s['error']} errors | {s['skipped']} skipped{C.RESET}")

    def run(self):
        self.print_banner()

        if not self.check_dependencies():
            sys.exit(1)

        log("Ready. Paste AI response at the prompt.")
        print()

        try:
            while self.running:
                print(f"{C.CYAN}>{C.RESET} ", end="", flush=True)

                lines = []
                try:
                    line = input()
                    if line.lower() in ("quit", "q", "exit"):
                        self.running = False
                        break
                    lines.append(line)
                    # Read any additional lines from paste
                    while select.select([sys.stdin], [], [], 0.3)[0]:
                        l = sys.stdin.readline()
                        if not l:
                            break
                        lines.append(l.rstrip())
                except EOFError:
                    break

                if not lines:
                    continue

                pasted = "\n".join(lines)

                code = extract_code_block(pasted)
                if not code:
                    # No fenced block — treat entire paste as the command
                    code = pasted.strip()
                    if not code:
                        continue
                    info("No fenced block — running paste directly.")

                if code == self.last_command:
                    warn("Same command as last time — skipping.")
                    continue

                self.last_command = code

                if not self.confirm_run(code):
                    continue

                log("Running...")
                result = run_command_captured(code)
                formatted = self.handle_result(result)

                if self.running:
                    set_clipboard(formatted)
                    ok("Result copied to clipboard — paste into your AI chat.")
                    print()

        except KeyboardInterrupt:
            print()
            log("Pastey stopped.")
        finally:
            self.print_stats()

if __name__ == "__main__":
    Pastey().run()
