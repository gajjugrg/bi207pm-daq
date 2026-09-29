Option Explicit

' ============================================================
'  LeCroy X-Stream histogram snapshot logger  -  v2 compact format
'
'  Every INTERVAL_MINUTES: clear the scope's sweeps, let the histograms
'  in math functions FUNC_NAMES accumulate, then write ONE file:
'
'     C:\Histograms\<YEAR>_<MON>\Record_<YEAR>_<MON>_<DD>_<HH>_<MM>.csv
'     (".csv.gz" when GZIP_OUTPUT = True)
'
'  File layout - a few "#" header lines, then ONE line per function:
'
'     #lecroy-histograms v2
'     #timestamp=2026-09-06T14:00:00
'     #interval_min=60
'     #columns=name,binWidth,offset,firstBin,lastBin,nBinsTotal,sum,counts...
'     F1,1.25E-10,-1.2E-07,412,1587,2000,3600000,0,3,7,12,...
'     F2,...
'     F5,unavailable
'
'  Only bin populations are stored (scope bins firstBin..lastBin, inclusive).
'  The bin axis is rebuilt by the reader (lecroy_hist.py):
'        centre of bin j = offset + binWidth * (j + shift)
'  shift = 0 is the convention of the old logger; 0.5 if OffsetAtLeftEdge
'  really is the left edge of bin 0. Nothing about this choice is baked
'  into the file.
'  "sum" is the total of the stored counts - an integrity check used by
'  "python lecroy_hist.py verify".
' ============================================================

' ---------------- settings ----------------
Const BASE_FOLDER      = "C:\Histograms"
Const INTERVAL_MINUTES = 60
Const GZIP_OUTPUT      = True     ' compress every file with PowerShell/.NET
Const POLL_SLEEP_MS    = 500
Const POLL_MAX_TRIES   = 40       ' 40 x 0.5 s = 20 s max wait per function

Dim FUNC_NAMES
FUNC_NAMES = Array("F1", "F2", "F3", "F4", "F5", "F6")   ' add "F7","F8" here if needed

Dim MONTH_NAMES
MONTH_NAMES = Array("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")

Dim scope, fso, shell
Set scope = CreateObject("LeCroy.XStreamDSO")
Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' ============================================================
' SMALL HELPERS
' ============================================================

Function ZeroPad(n)
    ZeroPad = Right("0" & CStr(n), 2)
End Function

Function IsoTimestamp(t)
    IsoTimestamp = Year(t) & "-" & ZeroPad(Month(t)) & "-" & ZeroPad(Day(t)) & "T" & _
                   ZeroPad(Hour(t)) & ":" & ZeroPad(Minute(t)) & ":" & ZeroPad(Second(t))
End Function

Function BuildFolderPath(t)
    BuildFolderPath = BASE_FOLDER & "\" & Year(t) & "_" & MONTH_NAMES(Month(t) - 1)
End Function

Function BuildFilePath(t)
    BuildFilePath = BuildFolderPath(t) & "\Record_" & Year(t) & "_" & MONTH_NAMES(Month(t) - 1) & _
                    "_" & ZeroPad(Day(t)) & "_" & ZeroPad(Hour(t)) & "_" & ZeroPad(Minute(t)) & ".csv"
End Function

Sub EnsureFolder(path)
    If Not fso.FolderExists(path) Then fso.CreateFolder path
End Sub

' Force a dot decimal separator regardless of the PC's locale, otherwise a
' European-configured scope PC writes "1,5" and shifts every field.
Function NumStr(v)
    NumStr = Replace(CStr(v), ",", ".")
End Function

' Parse a number from a value that may arrive as a string with units ("1.5ns").
Function ParseNumber(x)
    On Error Resume Next
    Dim s, i, ch, out, started
    s = CStr(x) : out = "" : started = False
    For i = 1 To Len(s)
        ch = Mid(s, i, 1)
        If InStr("0123456789+-.Ee", ch) > 0 Then
            out = out & ch
            started = True
        ElseIf started Then
            Exit For
        End If
    Next
    ParseNumber = 0
    If out <> "" And out <> "+" And out <> "-" And out <> "." Then ParseNumber = CDbl(out)
    If Err.Number <> 0 Then
        Err.Clear
        ParseNumber = 0
    End If
    On Error GoTo 0
End Function

' Convert a COM SAFEARRAY to a plain 0-based VBScript array.
' Returns Empty on failure - callers check with IsEmpty().
Function ToArray(safeArray)
    On Error Resume Next
    Dim tmp(), v, n
    n = 0
    For Each v In safeArray
        ReDim Preserve tmp(n)
        tmp(n) = v
        n = n + 1
    Next
    If Err.Number <> 0 Or n = 0 Then
        Err.Clear
        ToArray = Empty
    Else
        ToArray = tmp
    End If
    On Error GoTo 0
End Function

' ============================================================
' SCOPE ACCESS
' ============================================================

' True once the histogram has more than one populated bin.
Function HistogramReady(h)
    On Error Resume Next
    HistogramReady = (h.LastPopulatedBin > h.FirstPopulatedBin)
    If Err.Number <> 0 Then
        Err.Clear
        HistogramReady = False
    End If
    On Error GoTo 0
End Function

' Return the histogram result object of a math function ("F1"...),
' or Nothing if it is not ready within POLL_MAX_TRIES * POLL_SLEEP_MS.
Function PollHistogram(funcName)
    Dim h, tries
    Set PollHistogram = Nothing
    For tries = 1 To POLL_MAX_TRIES
        On Error Resume Next
        Set h = scope.Math.Functions(funcName).Out.Result
        If Err.Number <> 0 Then
            Err.Clear
            Set h = Nothing
        End If
        On Error GoTo 0
        If Not (h Is Nothing) Then
            If HistogramReady(h) Then
                Set PollHistogram = h
                Exit Function
            End If
        End If
        WScript.Sleep POLL_SLEEP_MS
    Next
End Function

' Build the file line for one function:
'   name,binWidth,offset,firstBin,lastBin,nBinsTotal,sum,c(firstBin),...,c(lastBin)
' Returns "" if the histogram is not available.
Function HistogramLine(funcName)
    Dim h, pop, firstBin, lastBin, width, offset, total, j
    Dim parts()
    HistogramLine = ""

    Set h = PollHistogram(funcName)
    If h Is Nothing Then Exit Function

    pop = ToArray(h.BinPopulations)
    If IsEmpty(pop) Then Exit Function

    firstBin = CLng(ParseNumber(h.FirstPopulatedBin))
    lastBin  = CLng(ParseNumber(h.LastPopulatedBin))
    width    = ParseNumber(h.BinWidth)
    offset   = ParseNumber(h.OffsetAtLeftEdge)

    If firstBin < 0 Then firstBin = 0                     ' keep indices inside the array
    If lastBin > UBound(pop) Then lastBin = UBound(pop)
    If width = 0 Or lastBin <= firstBin Then Exit Function

    ReDim parts(lastBin - firstBin)
    total = 0
    For j = firstBin To lastBin
        parts(j - firstBin) = NumStr(pop(j))
        total = total + CDbl(pop(j))
    Next

    HistogramLine = funcName & "," & NumStr(width) & "," & NumStr(offset) & "," & _
                    firstBin & "," & lastBin & "," & (UBound(pop) + 1) & "," & _
                    NumStr(total) & "," & Join(parts, ",")
End Function

' ============================================================
' COMPRESSION (optional)
' ============================================================

' Compress "path" to "path.gz" with the .NET GZipStream class through
' PowerShell (present on every Windows 7/10 scope PC), then decompress the
' result again and check it has exactly the original length. Only if that
' check passes is the uncompressed file deleted. Returns the path to use.
' Exit codes from PowerShell: 0 = ok, 1 = exception, 2 = length mismatch.
Function GzipFile(path)
    Dim gzPath, script, rc
    GzipFile = path
    gzPath   = path & ".gz"

    script = "$ErrorActionPreference='Stop';try{" & _
             "$i=[IO.File]::OpenRead('" & path & "');$L=$i.Length;" & _
             "$o=[IO.File]::Create('" & gzPath & "');" & _
             "$g=New-Object IO.Compression.GZipStream($o,[IO.Compression.CompressionMode]::Compress);" & _
             "$b=New-Object byte[] 65536;" & _
             "while(($n=$i.Read($b,0,$b.Length)) -gt 0){$g.Write($b,0,$n)};" & _
             "$g.Close();$o.Close();$i.Close();" & _
             "$c=New-Object IO.Compression.GZipStream([IO.File]::OpenRead('" & gzPath & "')," & _
             "[IO.Compression.CompressionMode]::Decompress);" & _
             "$t=0;while(($n=$c.Read($b,0,$b.Length)) -gt 0){$t+=$n};$c.Close();" & _
             "if($t -ne $L){exit 2};exit 0}catch{exit 1}"

    On Error Resume Next
    rc = shell.Run("powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command """ & _
                   script & """", 0, True)           ' 0 = hidden window, True = wait for exit
    If Err.Number <> 0 Then rc = -1
    Err.Clear
    On Error GoTo 0

    If rc = 0 And fso.FileExists(gzPath) Then
        If fso.GetFile(gzPath).Size > 0 Then
            fso.DeleteFile path
            GzipFile = gzPath
            Exit Function
        End If
    End If

    WScript.Echo "  gzip failed (exit code " & rc & "), kept the uncompressed file"
    On Error Resume Next
    If fso.FileExists(gzPath) Then fso.DeleteFile gzPath   ' drop a partial/empty .gz
    On Error GoTo 0
End Function

' ============================================================
' MAIN LOOP
' ============================================================

Dim nowTime, filePath, outFile, i, line, nSaved

EnsureFolder BASE_FOLDER
WScript.Echo "Logger started at " & Now & "  (interval=" & INTERVAL_MINUTES & " min, " & _
             (UBound(FUNC_NAMES) + 1) & " functions, gzip=" & GZIP_OUTPUT & ")"

Do
    On Error Resume Next
    scope.ClearSweeps()
    On Error GoTo 0

    WScript.Sleep INTERVAL_MINUTES * 60 * 1000

    nowTime  = Now
    filePath = BuildFilePath(nowTime)
    EnsureFolder BuildFolderPath(nowTime)

    Set outFile = fso.CreateTextFile(filePath, True)
    outFile.WriteLine "#lecroy-histograms v2"
    outFile.WriteLine "#timestamp=" & IsoTimestamp(nowTime)
    outFile.WriteLine "#interval_min=" & INTERVAL_MINUTES
    outFile.WriteLine "#columns=name,binWidth,offset,firstBin,lastBin,nBinsTotal,sum,counts..."

    nSaved = 0
    For i = 0 To UBound(FUNC_NAMES)
        line = HistogramLine(FUNC_NAMES(i))
        If line = "" Then
            outFile.WriteLine FUNC_NAMES(i) & ",unavailable"
        Else
            outFile.WriteLine line
            nSaved = nSaved + 1
        End If
    Next
    outFile.Close

    If GZIP_OUTPUT Then filePath = GzipFile(filePath)
    WScript.Echo "Saved: " & filePath & "  (" & nSaved & "/" & (UBound(FUNC_NAMES) + 1) & " histograms)"
Loop

Set scope = Nothing
Set fso   = Nothing
Set shell = Nothing
