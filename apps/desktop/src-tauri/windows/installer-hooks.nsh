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
; removes it too. An update keeps it: only what changed is downloaded again. Your meetings,
; voices and settings in %LOCALAPPDATA%\ZachCap are never touched.
!macro NSIS_HOOK_PREUNINSTALL
  ${If} $UpdateMode <> 1
    RMDir /r "$INSTDIR\engine\.venv"
  ${EndIf}
!macroend
