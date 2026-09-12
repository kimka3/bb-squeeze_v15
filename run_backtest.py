from pathlib import Path
import subprocess,sys
root=Path(__file__).resolve().parent
for args in [['src/risk_sweep.py','--design'],['src/risk_sweep.py'],['-m','unittest','discover','-s','tests'],['src/risk_sweep.py','--analyze'],['src/export_cap_report.py'],['src/monthly_report.py']]:
 subprocess.run([sys.executable,*args],cwd=root,check=True)
