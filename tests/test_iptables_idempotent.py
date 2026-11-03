import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = ROOT / "Coding" / "XX-Net-5.16.7.1" / "start"

CHAIN_RULES = [
    ["-d", "0.0.0.0/8", "-j", "RETURN"],
    ["-d", "10.0.0.0/8", "-j", "RETURN"],
    ["-d", "127.0.0.0/8", "-j", "RETURN"],
    ["-d", "169.254.0.0/16", "-j", "RETURN"],
    ["-d", "172.16.0.0/12", "-j", "RETURN"],
    ["-d", "192.168.0.0/16", "-j", "RETURN"],
    ["-d", "224.0.0.0/4", "-j", "RETURN"],
    ["-d", "240.0.0.0/4", "-j", "RETURN"],
    ["-p", "tcp", "-j", "REDIRECT", "--to-ports", "8086"],
]

FAKE_IPTABLES = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["FAKE_IPTABLES_STATE"])
if state_path.exists():
    state = json.loads(state_path.read_text(encoding="utf-8"))
else:
    state = {"chains": {}, "prerouting": []}

args = sys.argv[1:]
if args[:2] == ["-t", "nat"]:
    args = args[2:]
if not args:
    sys.exit(2)

command = args[0]
chain = args[1] if len(args) > 1 else None
rule = args[2:]
chains = state["chains"]
prerouting = state["prerouting"]
result = 0

if os.environ.get("FAKE_IPTABLES_FAIL") == "%s %s" % (command, chain):
    sys.exit(1)


def interface(arguments):
    for option in ("-i", "--in-interface"):
        if option in arguments:
            return arguments[arguments.index(option) + 1]
    return None


if command == "-N":
    if chain in chains:
        result = 1
    else:
        chains[chain] = []
elif command == "-F":
    if chain not in chains:
        result = 1
    else:
        chains[chain] = []
elif command == "-X":
    if chain not in chains or chains[chain] or prerouting:
        result = 1
    else:
        del chains[chain]
elif command == "-A" and chain == "REDSOCKS":
    if chain not in chains:
        result = 1
    else:
        chains[chain].append(rule)
elif command == "-A" and chain == "PREROUTING":
    prerouting.append(interface(rule))
elif command == "-C" and chain == "PREROUTING":
    result = 0 if interface(rule) in prerouting else 1
elif command == "-D" and chain == "PREROUTING":
    target = interface(rule)
    if target not in prerouting:
        result = 1
    else:
        prerouting.remove(target)
elif command == "-D" and chain == "REDSOCKS":
    if chain not in chains or rule not in chains[chain]:
        result = 1
    else:
        chains[chain].remove(rule)
elif command == "-S" and chain == "PREROUTING":
    for item in prerouting:
        print("-A PREROUTING -i %s -p tcp -j REDSOCKS" % item)
elif command == "-S" and chain is None:
    for name in chains:
        print("-N %s" % name)
    for item in prerouting:
        print("-A PREROUTING -i %s -p tcp -j REDSOCKS" % item)
else:
    result = 2

state_path.write_text(json.dumps(state), encoding="utf-8")
sys.exit(result)
'''


class IptablesIdempotentTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        temp_path = Path(self.temp_dir.name)
        self.state_path = temp_path / "state.json"
        fake_bin = temp_path / "bin"
        fake_bin.mkdir()
        fake_iptables = fake_bin / "iptables"
        fake_iptables.write_text(FAKE_IPTABLES, encoding="utf-8")
        fake_iptables.chmod(0o755)

        source = START_SCRIPT.read_text(encoding="utf-8")
        functions = re.search(
            r"(?ms)^set_iptables\(\)\{.*?^\}\n\nunset_iptables\(\)\{.*?^\}",
            source,
        )
        self.assertIsNotNone(functions, "无法从 start 抽取目标函数")
        dispatcher = re.search(r"(?ms)^ARGS=\$@.*?^done$", source)
        self.assertIsNotNone(dispatcher, "无法从 start 抽取参数分派")
        self.functions = functions.group(0)
        self.dispatcher = dispatcher.group(0)
        self.env = os.environ.copy()
        self.env["PATH"] = str(fake_bin) + os.pathsep + self.env["PATH"]
        self.env["FAKE_IPTABLES_STATE"] = str(self.state_path)

    def seed(self, interfaces, chain_rules=None):
        chains = {}
        if chain_rules is not None:
            chains["REDSOCKS"] = chain_rules
        self.state_path.write_text(
            json.dumps({"chains": chains, "prerouting": interfaces}),
            encoding="utf-8",
        )

    def state(self):
        if not self.state_path.exists():
            return {"chains": {}, "prerouting": []}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def run_function(self, function, interface=None, failure=None):
        args = [function] + ([interface] if interface is not None else [])
        env = self.env.copy()
        if failure is not None:
            env["FAKE_IPTABLES_FAIL"] = failure
        return subprocess.run(
            ["/bin/sh", "-s", "--"] + args,
            input=self.functions + '\n"$@"\n',
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
            check=False,
        )

    def run_dispatch_unset(self):
        script = self.functions + "\nos_name=Linux\nHANGUP=0\n" + self.dispatcher + "\n"
        return subprocess.run(
            ["/bin/sh", "-s", "--", "unset_iptables", "br-lan"],
            input=script,
            text=True,
            capture_output=True,
            env=self.env,
            timeout=5,
            check=False,
        )

    def test_repeated_set_converges_to_canonical_rules(self):
        first = self.run_function("set_iptables")
        second = self.run_function("set_iptables")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        state = self.state()
        self.assertEqual(state["prerouting"], ["br-lan"])
        self.assertEqual(state["chains"]["REDSOCKS"], CHAIN_RULES)

    def test_unset_removes_every_duplicate_jump(self):
        self.seed(["br-lan", "br-lan", "br-lan"], list(CHAIN_RULES))

        result = self.run_function("unset_iptables")

        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual(state["prerouting"], [])
        self.assertNotIn("REDSOCKS", state["chains"])

    def test_unset_preserves_chain_used_by_other_interface(self):
        self.seed(["br-lan", "eth0"], list(CHAIN_RULES))

        result = self.run_function("unset_iptables")

        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual(state["prerouting"], ["eth0"])
        self.assertEqual(state["chains"]["REDSOCKS"], CHAIN_RULES)

    def test_unset_without_existing_rules_succeeds(self):
        result = self.run_function("unset_iptables")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state(), {"chains": {}, "prerouting": []})

    def test_set_reports_chain_rule_failure(self):
        result = self.run_function("set_iptables", failure="-A REDSOCKS")

        self.assertNotEqual(result.returncode, 0)

    def test_set_reports_prerouting_append_failure(self):
        result = self.run_function("set_iptables", failure="-A PREROUTING")

        self.assertNotEqual(result.returncode, 0)

    def test_unset_reports_prerouting_list_failure(self):
        self.seed(["br-lan"], list(CHAIN_RULES))

        result = self.run_function("unset_iptables", failure="-S PREROUTING")

        self.assertNotEqual(result.returncode, 0)

    def test_unset_reports_delete_failure_when_jump_remains(self):
        self.seed(["br-lan"], list(CHAIN_RULES))

        result = self.run_function("unset_iptables", failure="-D PREROUTING")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()["prerouting"], ["br-lan"])

    def test_unset_reports_chain_flush_failure(self):
        self.seed(["br-lan"], list(CHAIN_RULES))

        result = self.run_function("unset_iptables", failure="-F REDSOCKS")

        self.assertNotEqual(result.returncode, 0)

    def test_unset_dispatch_returns_zero(self):
        self.seed(["br-lan"], list(CHAIN_RULES))

        result = self.run_dispatch_unset()

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
