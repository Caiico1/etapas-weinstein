' Lanza simulador_web.bat sin abrir ventana (lo usa la tarea de Windows "Simulador web" al iniciar sesion).
Set sh = CreateObject("WScript.Shell")
sh.Run """" & Replace(WScript.ScriptFullName, ".vbs", ".bat") & """", 0, False
