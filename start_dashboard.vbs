' Starts the Bedrock observability dashboard in the background (no console window).
' Installed in the Windows Startup folder by install_autostart.ps1; safe to double-click.
' The dashboard is then at http://localhost:8501. Log: app\streamlit.log
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root

If Not AlreadyRunning() Then
    sh.Run "cmd /c """"" & root & "\.venv\Scripts\streamlit.exe"" run app\dashboard.py > app\streamlit.log 2>&1""", 0, False
End If

Function AlreadyRunning()
    ' Kept in its own function: with On Error Resume Next, a failing If condition would run the If body.
    Dim http, status
    AlreadyRunning = False
    status = 0
    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP")
    http.setTimeouts 1000, 1000, 1000, 1000
    http.Open "GET", "http://localhost:8501/_stcore/health", False
    http.Send
    If Err.Number = 0 Then status = http.Status
    Err.Clear
    On Error GoTo 0
    If status = 200 Then AlreadyRunning = True
End Function
