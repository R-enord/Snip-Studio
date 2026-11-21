' Runs START_RECORDER.bat with no visible window.
Set sh = CreateObject("WScript.Shell")
d = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run """" & d & "\START_RECORDER.bat""", 0, False
