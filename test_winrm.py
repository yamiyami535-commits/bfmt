import winrm
s = winrm.Session('10.67.24.220', auth=('User', '123456'), transport='basic')
r = s.run_ps('Restart-Service "Automation Anywhere Bot Agent"')
print('OUT:', r.std_out.decode())
print('ERR:', r.std_err.decode())
print('CODE:', r.status_code)