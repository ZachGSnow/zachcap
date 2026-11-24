; Hooks for the NSIS installer (tauri.installer.conf.json).

; A per-user install goes to %LOCALAPPDATA%\ZachCap by default, which is where ZachCap keeps
; its meetings and database. The program goes to %LOCALAPPDATA%\Programs\ZachCap instead, so
; the two stay apart. A folder chosen in the installer is kept as chosen.
!macro NSIS_HOOK_PREINSTALL
  ${If} $INSTDIR == "$LOCALAPPDATA\${PRODUCTNAME}"
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PRODUCTNAME}"
    SetOutPath $INSTDIR
  ${EndIf}
!macroend

; The engine's venv is set up on first launch (a few GB) next to the program, so uninstalling
; removes it too. That happens after the uninstaller has closed ZachCap, and a moment later, so
; the engine (which exits with the app) isn't running from it; a venv left half removed is set
; up again on the next start (set_up in lib.rs). An update keeps it. Your meetings, voices and
; settings in %LOCALAPPDATA%\ZachCap are never touched.
!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $UpdateMode <> 1
    Sleep 3000
    RMDir /r "$INSTDIR\engine\.venv"
  ${EndIf}
!macroend
