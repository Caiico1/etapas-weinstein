' Lanza aave_aviso.bat sin abrir ventana (la tarea de Windows corre cada 15 minutos).
Set sh = CreateObject("WScript.Shell")
sh.Run """" & Replace(WScript.ScriptFullName, ".vbs", ".bat") & """", 0, True
