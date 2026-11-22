' Lanza simulador_aviso.bat sin abrir ventana (lo usa la tarea de Windows "Simulador calibracion").
Set sh = CreateObject("WScript.Shell")
sh.Run """" & Replace(WScript.ScriptFullName, ".vbs", ".bat") & """", 0, True
