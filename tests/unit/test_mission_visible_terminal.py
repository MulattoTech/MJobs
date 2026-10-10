"""No-provider tests for visible terminal and per-session Codex child limits."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from tools.agent_dashboard.mission_control import LaunchRequest, MissionControl
from tools.agent_dashboard.model_catalog import ModelCatalog, _choice
from tools.agent_dashboard.server import create_app
from tools.agent_dashboard.supervisor import AgentSupervisor
from tools.agent_dashboard.terminal_host import run_manifest


def git(path, *args):
    r = subprocess.run(["git", "-C", str(path), *args],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


class VisibleTerminalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/"repo"
        self.root.mkdir()
        git(self.root,"init")
        git(self.root,"checkout","-b","main")
        git(self.root,"-c","user.name=Test","-c","user.email=test@invalid.test",
            "commit","--allow-empty","-m","init")
        (self.root/"prompts").mkdir()
        (self.root/"prompts"/"fixture.md").write_text(
            "This is a multiline\nfeature implementation checklist\n",encoding="utf-8")
        self.supervisor=AgentSupervisor(self.root, mission_instance=True)
        self.supervisor.update_config("codex",{
            "launch_profile":"codex-luna-high",
            "launch_workspace":str(self.root),
            "prompt_file":"prompts/fixture.md",
            "model":"gpt-6-luna",
            "reasoning":"high",
            "agent_name":"Feature Engineer",
            "prompt_override":"Trace bug, implement API and UI.",
            "auto_continue":False,
            "launch_mode":"terminal",
            "max_subagents":12,
            "intended_subagents":8,
        })
        self.addCleanup(self._close_handles)

    def _close_handles(self):
        for child in list(self.supervisor._children.values()):
            if child.log_handle:
                child.log_handle.close()
        self.supervisor._children.clear()

    def test_codex_command_uses_stdin_and_enforces_maximum(self):
        prompt=self.supervisor._prompt("codex")
        cmd=self.supervisor._command("codex",self.root)
        self.assertEqual(cmd[-1],"-")
        self.assertNotIn(prompt,cmd)
        self.assertIn("agents.max_concurrent_threads_per_session=12",cmd)
        self.assertIn("features.multi_agent=true",cmd)
        self.assertIn("hard maximum 12",prompt)
        self.assertIn("intended active target 8",prompt)
        self.assertIn("Trace bug, implement API and UI.",prompt)
        self.assertNotIn("max_concurrent_threads_per_session=50",cmd)

    def test_zero_intended_disables_delegation_even_when_maximum_positive(self):
        self.supervisor.update_config("codex",{"intended_subagents":0})
        prompt=self.supervisor._prompt("codex")
        self.assertIn("intended active target 0",prompt)
        # Intended zero is a request for no delegation, so the native feature
        # is disabled too rather than relying solely on model compliance.
        self.assertIn("features.multi_agent=false",
                      self.supervisor._command("codex",self.root))
        self.supervisor.update_config("codex",{"max_subagents":0})
        self.assertNotIn("features.multi_agent=true",
                         self.supervisor._command("codex",self.root))

    def test_maximum_target_and_mode_validation(self):
        for item in [
            {"max_subagents":51},
            {"max_subagents":-1},
            {"max_subagents":4,"intended_subagents":8},
            {"intended_subagents":13},
            {"max_subagents":True},
            {"launch_mode":"detached"},
        ]:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.supervisor.update_config("codex",item)
        self.assertEqual(self.supervisor.lane_config("codex")["max_subagents"],12)
        self.assertEqual(self.supervisor.lane_config("codex")["intended_subagents"],8)
        with self.assertRaises(ValueError):
            self.supervisor.update_config("claude",{"max_subagents":2})

    def test_actual_multiline_prompt_survives_stdin_without_model(self):
        exe=Path(self.tmp.name)/"safe_stub.py"
        exe.write_text(
            "import json,sys\n"
            "print('SYNTHETIC_INPUT='+json.dumps(sys.stdin.read(),ensure_ascii=False))\n",
            encoding="utf-8",
        )
        text="Implement the **main product feature**.\nLine 2 with > | \"quotes\" % and Ω\n"
        manifest=Path(self.tmp.name)/"launch.json"
        log=Path(self.tmp.name)/"worker.log"
        manifest.write_text(json.dumps({
            "lane":"codex","argv":[sys.executable,str(exe),"-"],
            "prompt":text,"workspace":str(self.root),"log":str(log),
            "mode":"headless","hold_seconds":0,
        }),encoding="utf-8")
        self.assertEqual(run_manifest(manifest),0)
        self.assertFalse(manifest.exists(),"Prompt-bearing manifest must be erased")
        result=log.read_text(encoding="utf-8")
        line=next(line[len("SYNTHETIC_INPUT="):] for line in result.splitlines()
                  if line.startswith("SYNTHETIC_INPUT="))
        self.assertEqual(json.loads(line),text)
        self.assertIn("Approved prompt: "+str(len(text)),result)

    def test_windows_cmd_wrapper_preserves_multiline_stdin(self):
        if os.name!="nt":
            self.skipTest("npm .cmd wrappers are Windows-only")
        base=Path(self.tmp.name)
        stub=base/"wrapped_worker.py"
        stub.write_text(
            "import sys,json"+chr(10)+
            "print('FULL_STDIN='+json.dumps(sys.stdin.read(),ensure_ascii=False))"+chr(10),
            encoding="utf-8",
        )
        shim=base/"safe_npm_wrapper.cmd"
        shim.write_text(
            "@echo off"+chr(10)+
            '"'+sys.executable+'" "'+str(stub)+'" %*'+chr(10),
            encoding="utf-8",
        )
        prompt=chr(10).join([
            "First instruction.",
            'Second line with & | %% ! <caret> and quoted "text".',
            "",
        ])
        manifest=base/"stdin-through-cmd.json"
        log=base/"stdin-through-cmd.log"
        manifest.write_text(json.dumps({
            "lane":"codex","argv":["cmd.exe","/d","/s","/c",str(shim),"-"],
            "prompt":prompt,"workspace":str(self.root),"log":str(log),
            "mode":"headless","hold_seconds":0,
        }),encoding="utf-8")
        self.assertEqual(run_manifest(manifest),0)
        lines=log.read_text(encoding="utf-8").splitlines()
        restored=next(json.loads(x[len("FULL_STDIN="):])
                      for x in lines if x.startswith("FULL_STDIN="))
        self.assertEqual(restored,prompt)

    def test_visible_start_uses_true_windows_console_and_owned_manifest(self):
        if os.name!="nt":
            self.skipTest("Windows-only visible terminals")
        mock_process=MagicMock()
        mock_process.pid=33444
        mock_process.poll.return_value=None
        with patch.object(self.supervisor,"_launch_command",
                          return_value=[sys.executable,"fixture.py","-"]), \
             patch("tools.agent_dashboard.supervisor.subprocess.Popen",
                   return_value=mock_process) as launched:
            state=self.supervisor.start(
                "codex",manual=True,
                expected_prompt_hash=hashlib.sha256(
                    self.supervisor._prompt("codex").encode("utf-8")
                ).hexdigest(),
            )
        self.assertEqual(state["launch_mode"],"terminal")
        self.assertTrue(state["terminal_visible"])
        self.assertEqual(state["pid"],33444)
        self.assertEqual(launched.call_args.kwargs["creationflags"],
                         subprocess.CREATE_NEW_CONSOLE)
        self.assertEqual(launched.call_args.kwargs["startupinfo"].wShowWindow,1)
        cmd=launched.call_args.args[0]
        self.assertTrue(str(cmd[1]).endswith("terminal_host.py"))
        self.assertIn("--manifest",cmd)
        manifest=Path(cmd[-1])
        try:
            data=json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(data["argv"],[sys.executable,"fixture.py","-"])
            self.assertIn("intended active target 8",data["prompt"])
            self.assertEqual(data["mode"],"terminal")
            self.assertEqual(data["workspace"],str(self.root))
            self.assertFalse(data["argv"][-1]==data["prompt"])
        finally:
            manifest.unlink(missing_ok=True)

    def test_headless_start_does_not_open_a_console(self):
        self.supervisor.update_config("codex",{"launch_mode":"headless"})
        fake=MagicMock()
        fake.pid=45555
        fake.poll.return_value=None
        with patch.object(self.supervisor,"_launch_command",
                          return_value=[sys.executable,"fixture.py","-"]),\
             patch("tools.agent_dashboard.supervisor.subprocess.Popen",
                   return_value=fake) as launched:
            state=self.supervisor.start("codex")
        self.assertFalse(state["terminal_visible"])
        self.assertEqual(launched.call_args.kwargs["stdout"],subprocess.DEVNULL)
        self.assertEqual(launched.call_args.kwargs["stdin"],subprocess.DEVNULL)
        self.assertNotEqual(launched.call_args.kwargs["creationflags"],
                            getattr(subprocess,"CREATE_NEW_CONSOLE",-1))
        Path(launched.call_args.args[0][-1]).unlink(missing_ok=True)

    def test_log_viewer_never_spawns_a_second_coding_agent(self):
        if os.name!="nt":
            self.skipTest("Windows-only log viewer")
        self.supervisor.log_path("codex").write_text("Previous completed run\n",encoding="utf-8")
        fake=MagicMock()
        fake.pid=56666
        fake.poll.return_value=None
        with patch("tools.agent_dashboard.supervisor.subprocess.Popen",
                   return_value=fake) as spawn, \
             patch.object(self.supervisor,"_launch_command",
                          side_effect=AssertionError("Log viewer must not launch coding CLI")):
            first=self.supervisor.open_log_console("codex")
            second=self.supervisor.open_log_console("codex")
        self.assertEqual(first["pid"],56666)
        self.assertTrue(second["reused"])
        self.assertEqual(spawn.call_count,1)
        self.assertIn("terminal_viewer.py",str(spawn.call_args.args[0]))

    def test_launch_preview_fingerprint_includes_visibility_and_capacity(self):
        # Pure unit-level snapshot of real supervisor command/prompt.
        base=self.supervisor.lane_config("codex")
        base.update({"prompt_file":"prompts/fixture.md"})
        prompt=self.supervisor._prompt("codex",config_override=base)
        a=self.supervisor._command("codex",self.root,config_override=base)
        self.assertEqual(a[-1],"-")
        base["max_subagents"]=16
        base["intended_subagents"]=10
        changed=self.supervisor._command("codex",self.root,config_override=base)
        self.assertNotEqual(a,changed)
        self.assertNotEqual(prompt,self.supervisor._prompt("codex",config_override=base))


if __name__=="__main__":
    unittest.main()
