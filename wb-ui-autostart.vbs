' Launch the WorkBuddy Sync panel in the background, with no visible window.
'
' 为什么不用 pythonw.exe
' --------------------
' 本项目早期版本用 pythonw.exe（GUI 子系统、无控制台）启动面板。实测在部分
' 机器上会「端口已 listen，但任何请求都不响应」：进程在，浏览器一直转圈。
' 排查成本高、现象又像网络问题，因此改为 CONSOLE 版 python.exe，
' 再靠 Run 的窗口样式 0 把控制台窗口隐藏掉 —— 效果同样是"无窗口"，
' 但保留了正常的 stdout/stderr，行为可预期。
'
' 若你确认 pythonw 在本机可用，把下面的 PYEXE 改回 "pythonw.exe" 即可。
Option Explicit
Dim fso, shell, verDir, folder, best, cur, scriptDir, uiScript, pyExe
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

PYEXE = "python.exe"

verDir = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.workbuddy\binaries\python\versions"
best = ""

If fso.FolderExists(verDir) Then
  For Each folder In fso.GetFolder(verDir).SubFolders
    cur = folder.Path & "\" & PYEXE
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

' 0 = 隐藏窗口，False = 不等待退出
shell.Run """" & verDir & "\" & best & "\" & PYEXE & """ """ & uiScript & """", 0, False
