' Lanza aave_aviso.bat sin abrir ventana (lo usa la tarea de Windows «Aave aviso HF»).
Set sh = CreateObject("WScript.Shell")
sh.Run """" & Replace(WScript.ScriptFullName, ".vbs", ".bat") & """", 0, True
