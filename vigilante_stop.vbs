' Lanza vigilante_stop.bat sin abrir ventana (lo usa la tarea de Windows "Vigilante stop", cada hora).
Set sh = CreateObject("WScript.Shell")
sh.Run """" & Replace(WScript.ScriptFullName, ".vbs", ".bat") & """", 0, True
