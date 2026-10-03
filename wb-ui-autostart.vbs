' Launch WorkBuddy Sync panel hidden in background (no window).
' Locates the newest pythonw.exe under WorkBuddy's bundled Python,
' then starts wb-ui.py (same folder as this script) without a console.
Option Explicit
Dim fso, shell, verDir, folder, best, cur, scriptDir, uiScript
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

verDir = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.workbuddy\binaries\python\versions"
best = ""

If fso.FolderExists(verDir) Then
  For Each folder In fso.GetFolder(verDir).SubFolders
    cur = folder.Path & "\pythonw.exe"
    If fso.FileExists(cur) Then
      If best = "" Or folder.Name > best Then
        best = folder.Name
      End If
    End If
  Next
End If

If best = "" Then
  WScript.Quit
End If

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
uiScript = scriptDir & "\wb-ui.py"

If Not fso.FileExists(uiScript) Then
  WScript.Quit
End If

' 0 = hidden window, False = do not wait
shell.Run """" & verDir & "\" & best & "\pythonw.exe"" """ & uiScript & """", 0, False
