; The look and the pages of ZachCap's installer and uninstaller, in place of NSIS's own wizard:
; one page to start from, the progress, and one at the end, beside a panel with the app's logo.
; installer.nsi (Tauri's template) has only these pages; what they install is unchanged.
;
; The pictures are drawn by scripts/installer-art.mjs (windows/art) and made into bitmaps, one
; for each display scale, by build-installer.ps1 (ZC_ART). The buttons are NSIS's own, with a
; picture on them, so Enter, Tab and screen readers work as in any other installer.

!include nsDialogs.nsh
!include WinMessages.nsh

; In pixels at 100%: the window, its panel, and the margin of the pages.
!define ZC_WIDTH 640
!define ZC_HEIGHT 400
!define ZC_PANEL 220
!define ZC_MARGIN 36
!define ZC_FOOTER 84 ; the height of the part with the buttons
!define ZC_BUTTON 36 ; a button's height, with the 2 pixels of its picture that are cut off

!define ZC_INK 0x0A0A0A
!define ZC_MUTED 0x737373
!define ZC_PAPER 0xFFFFFF

!define ZC_GWL_STYLE -16
!define ZC_GWL_EXSTYLE -20
!define ZC_SS_BITMAP 0x0E
; BS_BITMAP | BS_FLAT | WS_CLIPSIBLINGS, without which a button may draw outside its region.
!define ZC_BS_PICTURE 0x04008080
!define ZC_SWP_PLAIN 0x0034 ; SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
!define ZC_EDGES 0x20200 ; WS_EX_CLIENTEDGE | WS_EX_STATICEDGE

; What NSIS calls its buttons, which it does once more when the copying failed: the last one then
; leaves. Each page gives them its own names (ZcButton).
MiscButtonText "" "" "Close" "Close"

Var ZcDpi
Var ZcArt ; the pictures' scale
Var ZcTitleFont
Var ZcTextFont
Var ZcSmallFont
Var ZcPage
Var ZcFound ; the version that is installed, or ""
Var ZcCase ; "fresh", "update", "same" or "older"
Var ZcFolderLabel
Var ZcCheck
Var ZcDesktop ; 1: put a shortcut on the desktop
Var ZcPanelPicture
Var ZcStatus ; under the progress bar: what is being done

; Pixels at 100% as pixels of this display.
!macro ZcPx OUT PIXELS
  IntOp ${OUT} ${PIXELS} * $ZcDpi
  IntOp ${OUT} ${OUT} / 96
!macroend
!define ZcPx "!insertmacro ZcPx"

!macro ZcFont OUT PIXELS WEIGHT
  ${ZcPx} $R0 ${PIXELS}
  IntOp $R0 0 - $R0
  System::Call 'gdi32::CreateFontW(i R0, i 0, i 0, i 0, i ${WEIGHT}, i 0, i 0, i 0, i 1, i 0, i 0, i 5, i 0, w R1) p .s'
  Pop ${OUT}
!macroend

; Extracts a picture at the display's scale as $PLUGINSDIR\NAME.bmp.
!macro ZcPicture NAME
  ${If} $ZcArt = 200
    File "/oname=$PLUGINSDIR\${NAME}.bmp" "${ZC_ART}\${NAME}-200.bmp"
  ${ElseIf} $ZcArt = 175
    File "/oname=$PLUGINSDIR\${NAME}.bmp" "${ZC_ART}\${NAME}-175.bmp"
  ${ElseIf} $ZcArt = 150
    File "/oname=$PLUGINSDIR\${NAME}.bmp" "${ZC_ART}\${NAME}-150.bmp"
  ${ElseIf} $ZcArt = 125
    File "/oname=$PLUGINSDIR\${NAME}.bmp" "${ZC_ART}\${NAME}-125.bmp"
  ${Else}
    File "/oname=$PLUGINSDIR\${NAME}.bmp" "${ZC_ART}\${NAME}-100.bmp"
  ${EndIf}
!macroend

; A label of the page at X, Y (pixels at 100%), in a font and a color. Leaves it on the stack.
!macro ZcLabel X Y W H FONT COLOR TEXT
  ${ZcPx} $R0 ${X}
  ${ZcPx} $R1 ${Y}
  ${ZcPx} $R2 ${W}
  ${ZcPx} $R3 ${H}
  ${NSD_CreateLabel} $R0 $R1 $R2 $R3 "${TEXT}"
  Pop $R4
  SendMessage $R4 ${WM_SETFONT} ${FONT} 1
  SetCtlColors $R4 ${COLOR} ${ZC_PAPER}
  Push $R4
!macroend

; A button (its window in HWND) at X, Y as the picture NAME, WIDTH wide, called TEXT.
!macro ZcButton HWND NAME X Y WIDTH TEXT
  !insertmacro ZcPicture "button-${NAME}"
  !insertmacro ZcDress "${HWND}" "${NAME}" "${X}" "${Y}" "${WIDTH}" "${TEXT}"
!macroend
; The same, with its picture extracted already.
!macro ZcDress HWND NAME X Y WIDTH TEXT
  ${ZcPx} $R0 ${X}
  ${ZcPx} $R1 ${Y}
  ${ZcPx} $R2 ${WIDTH}
  ${ZcPx} $R3 ${ZC_BUTTON}
  ShowWindow ${HWND} ${SW_HIDE} ; or what is drawn of it meanwhile stays
  SendMessage ${HWND} ${WM_SETTEXT} 0 "STR:${TEXT}"
  System::Call 'uxtheme::SetWindowTheme(p ${HWND}, w " ", w " ")'
  System::Call 'user32::GetWindowLongW(p ${HWND}, i ${ZC_GWL_STYLE}) i .R4'
  IntOp $R4 $R4 | ${ZC_BS_PICTURE}
  System::Call 'user32::SetWindowLongW(p ${HWND}, i ${ZC_GWL_STYLE}, i R4)'
  System::Call 'user32::LoadImageW(p 0, w "$PLUGINSDIR\button-${NAME}.bmp", i 0, i R2, i R3, i 0x10) p .R5'
  SendMessage ${HWND} 0xF7 0 $R5 ; BM_SETIMAGE
  System::Call 'user32::SetWindowPos(p ${HWND}, p 0, i R0, i R1, i R2, i R3, i ${ZC_SWP_PLAIN})'
  ; Without the border Windows draws around it.
  ${ZcPx} $R6 2
  IntOp $R7 $R2 - $R6
  IntOp $R8 $R3 - $R6
  System::Call 'gdi32::CreateRectRgn(i R6, i R6, i R7, i R8) p .R9'
  System::Call 'user32::SetWindowRgn(p ${HWND}, p R9, i 1)'
  ShowWindow ${HWND} ${SW_SHOW}
  EnableWindow ${HWND} 1
!macroend

; The buttons at the bottom: the one that goes on, and the one that leaves.
!macro ZcPrimary NAME WIDTH TEXT
  GetDlgItem $R4 $HWNDPARENT 1
  StrCpy $0 $R4
  IntOp $1 ${ZC_WIDTH} - ${ZC_MARGIN}
  IntOp $1 $1 - ${WIDTH}
  IntOp $1 $1 - 2
  IntOp $2 ${ZC_HEIGHT} - ${ZC_MARGIN}
  IntOp $2 $2 - 34
  IntOp $3 ${WIDTH} + 4
  !insertmacro ZcButton $0 "${NAME}" $1 $2 $3 "${TEXT}"
!macroend
!macro ZcCancel BESIDE
  GetDlgItem $0 $HWNDPARENT 2
  IntOp $1 ${ZC_WIDTH} - ${ZC_MARGIN}
  IntOp $1 $1 - ${BESIDE}
  IntOp $1 $1 - 106
  IntOp $2 ${ZC_HEIGHT} - ${ZC_MARGIN}
  IntOp $2 $2 - 34
  !insertmacro ZcButton $0 "cancel" $1 $2 96 "Cancel"
!macroend
!macro ZcHide ID
  GetDlgItem $0 $HWNDPARENT ${ID}
  ShowWindow $0 ${SW_HIDE}
!macroend

; A checkbox of the page, ticked when STATE is 1. Leaves it in $ZcCheck.
!macro ZcCheckbox X Y W TEXT STATE
  ${ZcPx} $R0 ${X}
  ${ZcPx} $R1 ${Y}
  ${ZcPx} $R2 ${W}
  ${ZcPx} $R3 22
  ${NSD_CreateCheckbox} $R0 $R1 $R2 $R3 " ${TEXT}"
  Pop $ZcCheck
  SendMessage $ZcCheck ${WM_SETFONT} $ZcTextFont 1
  SetCtlColors $ZcCheck ${ZC_INK} ${ZC_PAPER}
  SendMessage $ZcCheck ${BM_SETCHECK} ${STATE} 0
!macroend

; A page of its own, white, with its title and the line under it.
!macro ZcPageStart TITLE SUBTITLE
  nsDialogs::Create 1018
  Pop $ZcPage
  SetCtlColors $ZcPage ${ZC_INK} ${ZC_PAPER}
  !insertmacro ZcHide 3 ; Back
  !insertmacro ZcLabel ${ZC_MARGIN} 40 350 34 $ZcTitleFont ${ZC_INK} "${TITLE}"
  Pop $R4
  !insertmacro ZcLabel ${ZC_MARGIN} 76 350 20 $ZcTextFont ${ZC_MUTED} "${SUBTITLE}"
  Pop $R4
!macroend

; The window, its panel and its fonts: once, before the first page. UN is "" or "un.".
!macro ZC_FUNCTIONS UN
Function ${UN}ZcWindow
  InitPluginsDir
  System::Call 'user32::GetDpiForWindow(p $HWNDPARENT) i .s'
  Pop $ZcDpi
  ${IfThen} $ZcDpi < 96 ${|} StrCpy $ZcDpi 96 ${|}
  ; The smallest pictures that needn't be stretched, or the largest there are.
  ${If} $ZcDpi <= 96
    StrCpy $ZcArt 100
  ${ElseIf} $ZcDpi <= 120
    StrCpy $ZcArt 125
  ${ElseIf} $ZcDpi <= 144
    StrCpy $ZcArt 150
  ${ElseIf} $ZcDpi <= 168
    StrCpy $ZcArt 175
  ${Else}
    StrCpy $ZcArt 200
  ${EndIf}

  ; Windows 11's own font, else Windows 10's.
  ReadRegStr $R2 HKLM "SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts" "Segoe UI Variable (TrueType)"
  StrCpy $R1 "Segoe UI"
  ${IfThen} $R2 != "" ${|} StrCpy $R1 "Segoe UI Variable Display" ${|}
  !insertmacro ZcFont $ZcTitleFont 25 600
  ${IfThen} $R2 != "" ${|} StrCpy $R1 "Segoe UI Variable Text" ${|}
  !insertmacro ZcFont $ZcTextFont 14 400
  !insertmacro ZcFont $ZcSmallFont 12 400

  ; NSIS's own header, lines and Back have no place here (it shows Back again at times, so that
  ; is put where it isn't seen).
  GetDlgItem $0 $HWNDPARENT 3
  System::Call 'user32::SetWindowPos(p r0, p 0, i -2000, i -2000, i 0, i 0, i 0x0015)'
  !insertmacro ZcHide 3
  !insertmacro ZcHide 1028
  !insertmacro ZcHide 1256
  !insertmacro ZcHide 1034
  !insertmacro ZcHide 1035
  !insertmacro ZcHide 1036
  !insertmacro ZcHide 1037
  !insertmacro ZcHide 1038
  !insertmacro ZcHide 1039
  !insertmacro ZcHide 1045
  !insertmacro ZcHide 1046
  SetCtlColors $HWNDPARENT ${ZC_INK} ${ZC_PAPER}

  ; The window, in the middle of where it was.
  System::Call '*(i, i, i, i) p .r9'
  System::Call 'user32::GetWindowRect(p $HWNDPARENT, p r9)'
  System::Call '*$9(i .r1, i .r2, i .r3, i .r4)'
  System::Call 'user32::GetClientRect(p $HWNDPARENT, p r9)'
  System::Call '*$9(i, i, i .r5, i .r6)'
  System::Free $9
  IntOp $3 $3 - $1 ; its width and height
  IntOp $4 $4 - $2
  ${ZcPx} $7 ${ZC_WIDTH}
  ${ZcPx} $8 ${ZC_HEIGHT}
  IntOp $7 $7 + $3
  IntOp $7 $7 - $5 ; with its frame
  IntOp $8 $8 + $4
  IntOp $8 $8 - $6
  IntOp $5 $3 - $7
  IntOp $5 $5 / 2
  IntOp $1 $1 + $5
  IntOp $6 $4 - $8
  IntOp $6 $6 / 2
  IntOp $2 $2 + $6
  System::Call 'user32::SetWindowPos(p $HWNDPARENT, p 0, i r1, i r2, i r7, i r8, i 0x0014)'

  ; The panel, and the pages beside it.
  !insertmacro ZcPicture "side"
  ${ZcPx} $1 ${ZC_PANEL}
  ${ZcPx} $2 ${ZC_HEIGHT}
  System::Call 'user32::LoadImageW(p 0, w "$PLUGINSDIR\side.bmp", i 0, i r1, i r2, i 0x10) p .r3'
  System::Call 'user32::CreateWindowExW(i 0, w "STATIC", w "", i ${WS_CHILD}|${WS_VISIBLE}|${ZC_SS_BITMAP}, i 0, i 0, i r1, i r2, p $HWNDPARENT, p 0, p 0, p 0) p .s'
  Pop $ZcPanelPicture
  SendMessage $ZcPanelPicture 0x172 0 $3 ; STM_SETIMAGE
  IntOp $3 ${ZC_WIDTH} - ${ZC_PANEL}
  IntOp $4 ${ZC_HEIGHT} - ${ZC_FOOTER}
  ${ZcPx} $3 $3
  ${ZcPx} $4 $4
  GetDlgItem $0 $HWNDPARENT 1018
  System::Call 'user32::SetWindowPos(p r0, p 0, i r1, i 0, i r3, i r4, i 0x0014)'
FunctionEnd

; The page with the progress: its title, a bar in the app's black, and what is being done.
Function ${UN}ZcProgress
  FindWindow $ZcPage "#32770" "" $HWNDPARENT
  SetCtlColors $ZcPage ${ZC_INK} ${ZC_PAPER}
  ; As large as the other pages: NSIS gives this one the size in its own template.
  IntOp $3 ${ZC_WIDTH} - ${ZC_PANEL}
  IntOp $4 ${ZC_HEIGHT} - ${ZC_FOOTER}
  ${ZcPx} $3 $3
  ${ZcPx} $4 $4
  System::Call 'user32::SetWindowPos(p $ZcPage, p 0, i 0, i 0, i r3, i r4, i 0x0016)'
  !insertmacro ZcHide 1
  !insertmacro ZcHide 2
  !insertmacro ZcHide 3
  GetDlgItem $0 $ZcPage 1027 ; Show details
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $ZcPage 1016 ; the details, shown if it fails
  ShowWindow $0 ${SW_HIDE}
  ${ZcPx} $1 ${ZC_MARGIN}
  ${ZcPx} $2 40
  ${ZcPx} $3 350
  ${ZcPx} $4 34
  StrCpy $5 "Installing ZachCap"
  ${IfThen} $ZcCase == "update" ${|} StrCpy $5 "Updating ZachCap" ${|}
  ${IfThen} "${UN}" != "" ${|} StrCpy $5 "Removing ZachCap" ${|}
  System::Call 'user32::CreateWindowExW(i 0, w "STATIC", w r5, i ${WS_CHILD}|${WS_VISIBLE}, i r1, i r2, i r3, i r4, p $ZcPage, p 0, p 0, p 0) p .r0'
  SendMessage $0 ${WM_SETFONT} $ZcTitleFont 1
  SetCtlColors $0 ${ZC_INK} ${ZC_PAPER}
  GetDlgItem $0 $ZcPage 1004 ; the bar
  System::Call 'uxtheme::SetWindowTheme(p r0, w " ", w " ")'
  System::Call 'user32::GetWindowLongW(p r0, i ${ZC_GWL_STYLE}) i .r5'
  IntOp $5 $5 | 1 ; PBS_SMOOTH
  System::Call 'user32::SetWindowLongW(p r0, i ${ZC_GWL_STYLE}, i r5)'
  System::Call 'user32::GetWindowLongW(p r0, i ${ZC_GWL_EXSTYLE}) i .r5'
  IntOp $6 ${ZC_EDGES} ~
  IntOp $5 $5 & $6
  System::Call 'user32::SetWindowLongW(p r0, i ${ZC_GWL_EXSTYLE}, i r5)'
  SendMessage $0 0x409 0 0x171717 ; PBM_SETBARCOLOR
  SendMessage $0 0x2001 0 0xEDEDED ; PBM_SETBKCOLOR
  ${ZcPx} $2 108
  ${ZcPx} $3 348
  ${ZcPx} $4 6
  System::Call 'user32::SetWindowPos(p r0, p 0, i r1, i r2, i r3, i r4, i ${ZC_SWP_PLAIN})'
  GetDlgItem $0 $ZcPage 1006 ; NSIS's own line, with each file's path
  ShowWindow $0 ${SW_HIDE}
  ${ZcPx} $2 126
  ${ZcPx} $4 36
  StrCpy $5 "This takes a few seconds."
  ${IfThen} "${UN}" != "" ${|} StrCpy $5 "The speech engine is a few GB, so this can take a minute." ${|}
  System::Call 'user32::CreateWindowExW(i 0, w "STATIC", w r5, i ${WS_CHILD}|${WS_VISIBLE}, i r1, i r2, i r3, i r4, p $ZcPage, p 0, p 0, p 0) p .s'
  Pop $ZcStatus
  SendMessage $ZcStatus ${WM_SETFONT} $ZcSmallFont 1
  SetCtlColors $ZcStatus ${ZC_MUTED} ${ZC_PAPER}
  !insertmacro ZcPicture "button-close" ; for ZcProgressLeave: after the copying it would be listed
FunctionEnd

; The copying is over: on to the last page by itself, or, if it didn't work, what was done, and
; Close.
Function ${UN}ZcProgressLeave
  IfAbort 0 done
  SendMessage $ZcStatus ${WM_SETTEXT} 0 "STR:It didn't work. This is what was done:"
  GetDlgItem $0 $ZcPage 1016
  ${ZcPx} $1 ${ZC_MARGIN}
  ${ZcPx} $2 154
  ${ZcPx} $3 348
  ${ZcPx} $4 150
  System::Call 'user32::SetWindowPos(p r0, p 0, i r1, i r2, i r3, i r4, i ${ZC_SWP_PLAIN})'
  ShowWindow $0 ${SW_SHOW}
  SendMessage $0 0x1004 0 0 $1 ; LVM_GETITEMCOUNT
  IntOp $1 $1 - 1
  SendMessage $0 0x1013 $1 0 ; LVM_ENSUREVISIBLE: the last line says why
  GetDlgItem $0 $HWNDPARENT 2
  IntOp $1 ${ZC_WIDTH} - ${ZC_MARGIN}
  IntOp $1 $1 - 114
  IntOp $2 ${ZC_HEIGHT} - ${ZC_MARGIN}
  IntOp $2 $2 - 34
  !insertmacro ZcDress $0 "close" $1 $2 116 "Close"
  done:
FunctionEnd
!macroend

; The installer's pages ----------------------------------------------------------------------

; Which version is there, and what this one is to it.
Function ZcWhatIsThere
  StrCpy $ZcCase "fresh"
  ReadRegStr $ZcFound SHCTX "${UNINSTKEY}" "DisplayVersion"
  ReadRegStr $R0 SHCTX "${UNINSTKEY}" "UninstallString"
  ${If} $ZcFound == ""
  ${OrIf} $R0 == ""
    StrCpy $ZcFound ""
    Return
  ${EndIf}
  nsis_tauri_utils::SemverCompare "${VERSION}" $ZcFound
  Pop $R0
  ${If} $R0 = 1
    StrCpy $ZcCase "update"
  ${ElseIf} $R0 = 0
    StrCpy $ZcCase "same"
  ${Else}
    StrCpy $ZcCase "older"
  ${EndIf}
FunctionEnd

Function ZcStart
  Call ZcWhatIsThere
  ${IfThen} $PassiveMode = 1 ${|} Abort ${|}
  ; As the install does itself (NSIS_HOOK_PREINSTALL): shown as it will be.
  ${If} $INSTDIR == "$LOCALAPPDATA\${PRODUCTNAME}"
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PRODUCTNAME}"
  ${EndIf}

  ${If} $ZcCase == "fresh"
    !insertmacro ZcPageStart "Install ZachCap" "Version ${VERSION}"
    !insertmacro ZcLabel ${ZC_MARGIN} 110 350 44 $ZcTextFont ${ZC_INK} "Records your calls and writes down who said what. The audio stays on this PC."
    Pop $R4
    !insertmacro ZcLabel ${ZC_MARGIN} 168 250 18 $ZcSmallFont ${ZC_MUTED} "Goes to"
    Pop $R4
    !insertmacro ZcLabel ${ZC_MARGIN} 186 244 20 $ZcTextFont ${ZC_INK} "$INSTDIR"
    Pop $ZcFolderLabel
    System::Call 'user32::GetWindowLongW(p $ZcFolderLabel, i ${ZC_GWL_STYLE}) i .R4'
    IntOp $R4 $R4 | 0x8000 ; SS_PATHELLIPSIS
    System::Call 'user32::SetWindowLongW(p $ZcFolderLabel, i ${ZC_GWL_STYLE}, i R4)'
    ${NSD_CreateButton} 0 0 10 10 "Change..."
    Pop $0
    !insertmacro ZcButton $0 "change" 290 176 96 "Change..."
    ${NSD_OnClick} $0 ZcChangeFolder
    !insertmacro ZcCheckbox ${ZC_MARGIN} 222 340 "Put a shortcut on the desktop" ${BST_CHECKED}
    !insertmacro ZcLabel ${ZC_MARGIN} 258 350 40 $ZcSmallFont ${ZC_MUTED} "For you only: no administrator rights needed. The first start downloads the speech libraries, about 4 GB."
    Pop $R4
    !insertmacro ZcPrimary "install" 112 "Install"
    !insertmacro ZcCancel 112
  ${ElseIf} $ZcCase == "update"
    !insertmacro ZcPageStart "Update ZachCap" "From $ZcFound to ${VERSION}"
    !insertmacro ZcLabel ${ZC_MARGIN} 116 350 66 $ZcTextFont ${ZC_INK} "Your meetings, voices and settings stay as they are. ZachCap closes for the update; a recording in progress is saved."
    Pop $R4
    !insertmacro ZcLabel ${ZC_MARGIN} 258 350 40 $ZcSmallFont ${ZC_MUTED} "The first start afterwards sets up the new speech engine, which takes up to a minute."
    Pop $R4
    !insertmacro ZcPrimary "update" 112 "Update"
    !insertmacro ZcCancel 112
  ${ElseIf} $ZcCase == "same"
    !insertmacro ZcPageStart "Reinstall ZachCap" "Version ${VERSION} is installed already"
    !insertmacro ZcLabel ${ZC_MARGIN} 116 350 66 $ZcTextFont ${ZC_INK} "The program's files are put back as they were. Your meetings, voices and settings stay as they are."
    Pop $R4
    !insertmacro ZcPrimary "reinstall" 112 "Reinstall"
    !insertmacro ZcCancel 112
  ${Else}
    !insertmacro ZcPageStart "An older ZachCap" "You have $ZcFound, and this is ${VERSION}"
    !insertmacro ZcLabel ${ZC_MARGIN} 116 350 88 $ZcTextFont ${ZC_INK} "An older version may not know what the newer one wrote in your library, such as which meetings are private. Installing the latest one instead is safer."
    Pop $R4
    !insertmacro ZcPrimary "anyway" 136 "Install anyway"
    !insertmacro ZcCancel 136
  ${EndIf}
  nsDialogs::Show
FunctionEnd

Function ZcChangeFolder
  Pop $R0 ; the button
  nsDialogs::SelectFolderDialog "Where ZachCap goes" "$INSTDIR"
  Pop $R0
  ${IfThen} $R0 == error ${|} Return ${|}
  StrCpy $R1 $R0 1 -1
  ${IfThen} $R1 == "\" ${|} StrCpy $R0 $R0 -1 ${|} ; a drive
  ; A folder of its own, as it's removed with the program.
  ${GetFileName} $R0 $R1
  ${IfThen} $R1 != "${PRODUCTNAME}" ${|} StrCpy $R0 "$R0\${PRODUCTNAME}" ${|}
  ${IfThen} $R0 == "$LOCALAPPDATA\${PRODUCTNAME}" ${|} StrCpy $R0 "$LOCALAPPDATA\Programs\${PRODUCTNAME}" ${|}
  StrCpy $INSTDIR $R0
  ${NSD_SetText} $ZcFolderLabel "$INSTDIR"
FunctionEnd

Function ZcStartLeave
  StrCpy $ZcDesktop 0
  ${If} $ZcCase == "fresh"
    ${NSD_GetState} $ZcCheck $R0
    ${IfThen} $R0 = ${BST_CHECKED} ${|} StrCpy $ZcDesktop 1 ${|}
  ${EndIf}
FunctionEnd

Function ZcFinish
  ${IfThen} $PassiveMode = 1 ${|} Abort ${|}
  ${If} $ZcCase == "update"
    !insertmacro ZcPageStart "ZachCap is up to date" "Version ${VERSION}"
    !insertmacro ZcLabel ${ZC_MARGIN} 116 350 66 $ZcTextFont ${ZC_INK} "What's new is in the app, under About. If Claude Desktop or Claude Code uses your meetings, restart it."
    Pop $R4
  ${ElseIf} $ZcCase == "fresh"
    !insertmacro ZcPageStart "ZachCap is installed" "Version ${VERSION}"
    !insertmacro ZcLabel ${ZC_MARGIN} 116 350 66 $ZcTextFont ${ZC_INK} "The first start sets up the speech engine. It downloads about 4 GB, which takes a few minutes."
    Pop $R4
  ${Else}
    !insertmacro ZcPageStart "ZachCap is installed" "Version ${VERSION}"
    !insertmacro ZcLabel ${ZC_MARGIN} 116 350 66 $ZcTextFont ${ZC_INK} "If Claude Desktop or Claude Code uses your meetings, restart it."
    Pop $R4
  ${EndIf}
  !insertmacro ZcCheckbox ${ZC_MARGIN} 200 340 "Open ZachCap" ${BST_CHECKED}
  !insertmacro ZcHide 2
  !insertmacro ZcPrimary "finish" 112 "Finish"
  nsDialogs::Show
FunctionEnd

Function ZcFinishLeave
  ${NSD_GetState} $ZcCheck $R0
  ${IfThen} $R0 = ${BST_CHECKED} ${|} Call RunMainBinary ${|}
FunctionEnd

; The uninstaller's pages ----------------------------------------------------------------------

Function un.ZcConfirm
  ${IfThen} $PassiveMode = 1 ${|} Abort ${|}
  !insertmacro ZcPageStart "Uninstall ZachCap" "Version ${VERSION}"
  !insertmacro ZcLabel ${ZC_MARGIN} 116 350 44 $ZcTextFont ${ZC_INK} "The program and its speech engine are removed. Your meetings, voices and settings stay in"
  Pop $R4
  !insertmacro ZcLabel ${ZC_MARGIN} 162 350 20 $ZcTextFont ${ZC_MUTED} "$LOCALAPPDATA\${PRODUCTNAME}"
  Pop $R4
  !insertmacro ZcCheckbox ${ZC_MARGIN} 200 350 "Also forget how its window was set up" ${BST_UNCHECKED}
  !insertmacro ZcPrimary "uninstall" 112 "Uninstall"
  !insertmacro ZcCancel 112
  nsDialogs::Show
FunctionEnd

Function un.ZcConfirmLeave
  ${NSD_GetState} $ZcCheck $DeleteAppDataCheckboxState
FunctionEnd

Function un.ZcDone
  ${IfThen} $PassiveMode = 1 ${|} Abort ${|}
  ${IfThen} $UpdateMode = 1 ${|} Abort ${|}
  !insertmacro ZcPageStart "ZachCap is removed" "Thanks for using it"
  !insertmacro ZcLabel ${ZC_MARGIN} 116 350 20 $ZcTextFont ${ZC_INK} "Your meetings, voices and settings are still in"
  Pop $R4
  !insertmacro ZcLabel ${ZC_MARGIN} 140 350 20 $ZcTextFont ${ZC_MUTED} "$LOCALAPPDATA\${PRODUCTNAME}"
  Pop $R4
  !insertmacro ZcHide 2
  !insertmacro ZcPrimary "close" 112 "Close"
  nsDialogs::Show
FunctionEnd
