#!/usr/bin/env python3
"""
Pastey_v1.0 — AI Terminal Bridge
Version: 1.0 (Supervised Mode)
Author: kron777
License: $9 lifetime — ko-fi.com/kron777
"""

import subprocess
import time
import sys
import re
import os
import signal
import threading
from datetime import datetime

HANG_TIMEOUT   = 120
MAX_RETRIES    = 3
POLL_INTERVAL  = 0.5

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

def get_clipboard():
    try:
        r = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                           capture_output=True, text=True, timeout=2)
        return r.stdout
    except Exception:
        return ""

def set_clipboard(text):
    try:
        proc = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE)
        proc.communicate(input=text.encode())
    except Exception:
        err("Could not write to clipboard — install xclip: sudo apt install xclip")

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
    output_file = "/tmp/pastey_output.txt"
    script_path = "/tmp/pastey_cmd.sh"
    exit_file   = "/tmp/pastey_exit.txt"

    wrapped = f"(\n{command}\n) > >(tee {output_file}) 2>&1\necho $? > {exit_file}\n"
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
        f"$ {result['command']}",
        result['output'].strip() if result['output'].strip() else "(no output)",
        "```",
        f"Exit code: {result['exit_code']} | Time: {result['duration']:.1f}s"
    ]
    if result['status'] == 'error':
        lines.append(f"\n⚠ Command failed with exit code {result['exit_code']}. Please fix.")
    elif result['status'] == 'timeout':
        lines.append(f"\n⚠ Command timed out after {HANG_TIMEOUT}s and was killed.")
    elif result['status'] == 'sudo':
        lines.append(f"\n⚠ Command requires sudo password.")
    elif result['status'] == 'crash':
        lines.append(f"\n⚠ Command crashed: {result['output']}")
    return "\n".join(lines)

class Pastey:
    def __init__(self):
        self.last_clipboard  = ""
        self.running         = True
        self.retry_count     = 0
        self.last_command    = ""
        self.session_stats   = {"run": 0, "ok": 0, "error": 0, "skipped": 0}

    def check_dependencies(self):
        missing = []
        for tool in ["xclip", "xdotool", "wmctrl"]:
            r = subprocess.run(["which", tool], capture_output=True)
            if r.returncode != 0:
                missing.append(tool)
        if missing:
            err(f"Missing tools: {', '.join(missing)}")
            err(f"Install with: sudo apt install {' '.join(missing)}")
            return False
        return True

    def confirm_run(self, code):
        print(f"\n{C.CYAN}{'='*60}{C.RESET}")
        print(f"{C.BOLD}PASTEY DETECTED CODE BLOCK:{C.RESET}")
        print(f"{C.CYAN}{'-'*60}{C.RESET}")
        for i, line in enumerate(code.splitlines(), 1):
            print(f"  {C.DIM}{i:3}{C.RESET}  {line}")
        print(f"{C.CYAN}{'-'*60}{C.RESET}")
        print(f"  {C.GREEN}[Enter]{C.RESET} Run it")
        print(f"  {C.YELLOW}[s]{C.RESET}     Skip")
        print(f"  {C.RED}[q]{C.RESET}     Quit")
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
        status = result["status"]
        self.session_stats["run"] += 1
        if status == "ok":
            ok(f"Done in {result['duration']:.1f}s")
            self.session_stats["ok"] += 1
            self.retry_count = 0
        elif status == "error":
            self.session_stats["error"] += 1
            self.retry_count += 1
            warn(f"Failed (attempt {self.retry_count}/{MAX_RETRIES})")
            if self.retry_count >= MAX_RETRIES:
                err("Max retries reached — stopping.")
                self.running = False
        elif status == "timeout":
            warn(f"Timed out after {HANG_TIMEOUT}s — killed.")
        elif status == "sudo":
            warn("sudo password needed — handle in terminal then press Enter.")
            input()
        elif status == "crash":
            err("Crashed!")
            self.running = False
        return format_result_for_ai(result)

    def print_banner(self):
        print(f"""
{C.CYAN}╔══════════════════════════════════════════════════════════╗
║  {C.BOLD}Pastey_v1.0 — AI Terminal Bridge{C.RESET}{C.CYAN}  Supervised Mode      ║
║  {C.DIM}Copy code from AI → Pastey runs it → copies result back{C.RESET}{C.CYAN}  ║
╚══════════════════════════════════════════════════════════╝{C.RESET}
""")

    def print_stats(self):
        s = self.session_stats
        print(f"\n{C.DIM}Session: {s['run']} run | {s['ok']} ok | {s['error']} errors | {s['skipped']} skipped{C.RESET}")

    def run(self):
        self.print_banner()
        if not self.check_dependencies():
            sys.exit(1)
        log("Watching clipboard for code blocks...")
        log("Copy any code block from your AI chat to trigger Pastey.")
        print()
        self.last_clipboard = get_clipboard()
        try:
            while self.running:
                time.sleep(POLL_INTERVAL)
                current = get_clipboard()
                if current == self.last_clipboard:
                    continue
                self.last_clipboard = current
                code = extract_code_block(current)
                if not code:
                    continue
                if code == self.last_command:
                    continue
                self.last_command = code
                if not self.confirm_run(code):
                    continue
                log("Running...")
                result = run_command_captured(code)
                formatted = self.handle_result(result)
                if self.running:
                    set_clipboard(formatted)
                    ok("Result copied to clipboard — paste into AI chat.")
                    print()
        except KeyboardInterrupt:
            print()
            log("Pastey stopped.")
        finally:
            self.print_stats()

if __name__ == "__main__":
    Pastey().run()
