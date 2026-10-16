"""Finite outer-process deadline used by the approved live design runner.

This bounds child startup, its single HTTP and MCP shutdown together. It does
not change or describe httpx operation timeouts as a total request deadline.
"""
import os,subprocess,time

def run_bounded(command,*,timeout_s,cwd,stdout):
    started=time.monotonic()
    child=subprocess.Popen(command,cwd=cwd,stdout=stdout,stderr=subprocess.STDOUT)
    timed_out=False
    try:code=child.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out=True
        if os.name=='nt':subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False,timeout=15)
        if child.poll() is None:child.kill()
        code=child.wait(timeout=15)
    return dict(pid=child.pid,exit_code=code,timed_out=timed_out,deadline_s=timeout_s,elapsed_s=time.monotonic()-started,enforcement='outer child process wallclock; single HTTP included; process tree terminated on timeout')
