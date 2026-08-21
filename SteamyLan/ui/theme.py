from __future__ import annotations






STYLE = r"""
/* ---------- Application canvas ---------- */
QMainWindow {
    background-color: #090e18;
}
QWidget {
    color: #eef3fb;
    background-color: transparent;
    font-family: "Segoe UI";
    font-size: 14px;
}
QWidget#AppRoot,
QWidget#PageBody,
QStackedWidget#ContentStack {
    background-color: #090e18;
}

/* Keep text labels genuinely transparent unless a named badge opts in. */
QLabel {
    background-color: transparent;
    border: none;
}

/* ---------- Chrome and surfaces ---------- */
QFrame#TopBar {
    background-color: #0d1422;
    border: none;
    border-bottom: 1px solid #202b3e;
}
QFrame#Hero {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 #14203a,
        stop:0.55 #111a2d,
        stop:1 #0f1727
    );
    border: 1px solid #293a58;
    border-radius: 16px;
}
QFrame#Card {
    background-color: #111a2a;
    border: 1px solid #26334a;
    border-radius: 14px;
}
QFrame#Card:hover {
    border-color: #33445f;
}
QFrame#ProcessCard {
    background-color: #101827;
    border: 1px solid #27354c;
    border-radius: 15px;
}
QFrame#ProcessCard:hover {
    background-color: #121c2d;
    border-color: #425778;
}
QFrame#Banner {
    background-color: #0e1b32;
    border: 1px solid #29486f;
    border-radius: 10px;
}
QFrame#WarningBanner {
    background-color: #251d12;
    border: 1px solid #66502c;
    border-radius: 10px;
}
QFrame#IconTile,
QLabel#IconTile {
    background-color: #172238;
    border: 1px solid #2c3b55;
    border-radius: 12px;
}

/* ---------- Typography ---------- */
QLabel#Brand {
    color: #ffffff;
    font-size: 21px;
    font-weight: 700;
}
QLabel#Title {
    color: #f8faff;
    font-size: 28px;
    font-weight: 700;
}
QLabel#Heading {
    color: #f3f6fb;
    font-size: 19px;
    font-weight: 700;
}
QLabel#Service {
    color: #f4f7fc;
    font-size: 17px;
    font-weight: 600;
}
QLabel#Muted {
    color: #a3afc2;
}
QLabel#Subtle {
    color: #77869e;
    font-size: 12px;
}
QLabel#Status {
    color: #e7edf8;
    font-weight: 600;
}
QLabel#Section {
    color: #8f9db4;
    font-size: 11px;
    font-weight: 700;
}
QProgressBar#LoadingBar {
    min-height: 7px;
    max-height: 7px;
    border: 1px solid #2f4770;
    border-radius: 4px;
    background-color: #101a2b;
}
QProgressBar#LoadingBar::chunk {
    border-radius: 3px;
    background-color: #7697ff;
}

/* ---------- Pills / badges ---------- */
QLabel#Identity {
    color: #c2cce0;
    background-color: #111b2c;
    border: 1px solid #2a3952;
    border-radius: 10px;
    padding: 6px 11px;
    font-size: 12px;
}
QLabel#Avatar {
    color: #d8e0ff;
    background-color: #1a2947;
    border: 1px solid #344d7b;
    border-radius: 23px;
    font-size: 18px;
    font-weight: 700;
}
QLabel#PortChip {
    color: #d6deff;
    background-color: #182640;
    border: 1px solid #304875;
    border-radius: 8px;
    padding: 4px 9px;
    font-size: 12px;
    font-weight: 600;
}
QLabel#CountChip {
    color: #bdc9ff;
    background-color: #17233e;
    border: 1px solid #2d4170;
    border-radius: 9px;
    padding: 3px 8px;
    font-size: 12px;
    font-weight: 600;
}

/* ---------- Buttons ---------- */
QPushButton {
    min-height: 38px;
    padding: 0 16px;
    color: #e8edf6;
    background-color: #141e30;
    border: 1px solid #33425a;
    border-radius: 9px;
    font-weight: 600;
}
QPushButton:hover {
    color: #ffffff;
    background-color: #19263a;
    border-color: #485b79;
}
QPushButton:pressed {
    background-color: #101928;
    border-color: #536784;
}
QPushButton:focus {
    border-color: #6d86ff;
}
QPushButton:disabled {
    color: #66748a;
    background-color: #0f1725;
    border-color: #222d3f;
}
QPushButton#Primary {
    min-height: 44px;
    padding: 0 20px;
    color: #ffffff;
    background-color: #6079f6;
    border: 1px solid #7289fa;
    border-radius: 10px;
    font-weight: 700;
}
QPushButton#Primary:hover {
    background-color: #6d85ff;
    border-color: #8296ff;
}
QPushButton#Primary:pressed {
    background-color: #526be6;
}
QPushButton#Primary:disabled {
    color: #748098;
    background-color: #162038;
    border-color: #293753;
}
QPushButton#Danger {
    color: #ffb5b3;
    background-color: #241719;
    border-color: #573036;
}
QPushButton#Danger:hover {
    color: #ffd0cf;
    background-color: #321c20;
    border-color: #7c4149;
}
QPushButton#Nav {
    min-height: 34px;
    padding: 0 15px;
    color: #9ba8bd;
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 9px;
    font-weight: 600;
}
QPushButton#Nav:hover {
    color: #eef3fb;
    background-color: #141f31;
    border-color: #202f46;
}
QPushButton#Nav:checked {
    color: #d7dfff;
    background-color: #1a2949;
    border-color: #324b79;
}
QPushButton#SettingsButton {
    min-height: 34px;
    padding: 0 14px;
    color: #dce4f2;
    background-color: #111a2a;
    border-color: #2c3b53;
}

/* ---------- Inputs ---------- */
QLineEdit,
QSpinBox,
QComboBox {
    min-height: 38px;
    padding: 0 11px;
    color: #eef3fb;
    background-color: #0d1523;
    border: 1px solid #31415b;
    border-radius: 9px;
    selection-color: #ffffff;
    selection-background-color: #526de2;
}
QLineEdit:hover,
QSpinBox:hover,
QComboBox:hover {
    border-color: #40526f;
}
QLineEdit:focus,
QSpinBox:focus,
QComboBox:focus {
    border-color: #6a83fb;
}
QLineEdit::placeholder {
    color: #708098;
}
QComboBox::drop-down {
    width: 28px;
    border: none;
}
QComboBox QAbstractItemView {
    color: #e8edf6;
    background-color: #101827;
    border: 1px solid #33425a;
    border-radius: 8px;
    outline: 0;
    padding: 4px;
    selection-color: #ffffff;
    selection-background-color: #263d70;
}
QComboBox QAbstractItemView::item {
    min-height: 28px;
    padding: 3px 9px;
    border-radius: 6px;
}
QComboBox QAbstractItemView::item:hover {
    color: #ffffff;
    background-color: #1a2946;
}
QComboBox QAbstractItemView::item:selected {
    color: #ffffff;
    background-color: #263d70;
}

/* ---------- Compact numeric stepper ---------- */
QFrame#Stepper {
    min-height: 34px;
    background-color: #0d1523;
    border: 1px solid #31415b;
    border-radius: 9px;
}
QFrame#Stepper:hover {
    border-color: #40526f;
}
QLineEdit#StepperValue {
    min-height: 28px;
    padding: 0 2px;
    color: #eef3fb;
    background-color: transparent;
    border: none;
    border-radius: 0;
    font-weight: 600;
}
QLineEdit#StepperValue:focus {
    border: none;
}
QLabel#StepperSuffix {
    color: #8f9db4;
    background-color: transparent;
    border: none;
    padding: 0 5px 0 1px;
    font-size: 12px;
    font-weight: 600;
}
QToolButton#StepperButton {
    min-width: 28px;
    max-width: 28px;
    min-height: 28px;
    max-height: 28px;
    padding: 0;
    color: #aebad0;
    background-color: transparent;
    border: none;
    border-radius: 7px;
    font-size: 17px;
    font-weight: 600;
}
QToolButton#StepperButton:hover {
    color: #ffffff;
    background-color: #1b2940;
}
QToolButton#StepperButton:pressed {
    background-color: #223451;
}
QToolButton#PasswordReveal {
    min-width: 48px;
    max-width: 48px;
    min-height: 30px;
    max-height: 30px;
    padding: 0;
    color: #aebad0;
    background-color: #111b2c;
    border: 1px solid #31415b;
    border-radius: 8px;
    font-size: 11px;
    font-weight: 600;
}
QToolButton#PasswordReveal:hover {
    color: #ffffff;
    background-color: #18253a;
    border-color: #475b79;
}
QToolButton#PasswordReveal:checked {
    color: #dce4ff;
    background-color: #182a4b;
    border-color: #526fae;
}
QToolButton#PasswordReveal:disabled {
    color: #5f6d83;
    background-color: #0f1725;
    border-color: #222d3f;
}

/* ---------- Checkboxes ---------- */
QCheckBox {
    min-height: 28px;
    spacing: 9px;
    color: #dfe6f1;
    background-color: transparent;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    background-color: #0c1421;
    border: 1px solid #465773;
    border-radius: 5px;
}
QCheckBox::indicator:hover {
    border-color: #627596;
}
QCheckBox::indicator:checked {
    background-color: #6079f6;
    border-color: #7e93fa;
}

/* ---------- Scroll areas ---------- */
QScrollArea,
QScrollArea > QWidget,
QScrollArea > QWidget > QWidget {
    background-color: transparent;
    border: none;
}
QScrollBar:vertical {
    width: 10px;
    margin: 4px 2px;
    background-color: transparent;
}
QScrollBar::handle:vertical {
    min-height: 34px;
    background-color: #2a3950;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background-color: #3b4d68;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    height: 0;
    background-color: transparent;
}

/* ---------- Secondary controls ---------- */
QToolButton {
    padding: 6px;
    color: #aab7ca;
    background-color: transparent;
    border: none;
    border-radius: 6px;
}
QToolButton:hover {
    color: #ffffff;
    background-color: #141f30;
}
QMenu {
    color: #edf2fa;
    background-color: #111a29;
    border: 1px solid #2f3d54;
    padding: 5px;
}
QMenu::item {
    padding: 8px 18px;
    border-radius: 6px;
}
QMenu::item:selected {
    background-color: #1e3050;
}
QToolTip {
    color: #eef3fb;
    background-color: #111a29;
    border: 1px solid #35445e;
    padding: 6px 8px;
}

/* ---------- Dialogs ---------- */
QDialog,
QMessageBox {
    background-color: #0d1421;
}
QDialogButtonBox {
    background-color: transparent;
}
QDialogButtonBox QPushButton {
    min-width: 90px;
}
QWidget#SettingsGroup {
    background-color: transparent;
}
QWidget#SettingsGroup QCheckBox {
    min-height: 24px;
}
QWidget#SettingsGroup QLabel#Subtle {
    margin-top: 0;
}
"""





STYLE += r"""
QWidget#NavRow,
QFrame#SubTabs {
    background-color: transparent;
    border: none;
}
QFrame#Footer {
    background-color: #0b111d;
    border: none;
    border-top: 1px solid #1d2839;
}
QLabel#FooterLink {
    color: #8492a8;
    background-color: transparent;
    font-size: 11px;
}
QLabel#DonateLink {
    color: #aebcff;
    background-color: transparent;
    font-size: 11px;
    font-weight: 600;
}
QLineEdit#DonationAddress {
    font-family: "Consolas";
    font-size: 12px;
}
QPushButton#SubTab {
    min-height: 32px;
    padding: 0 15px;
    color: #98a7bc;
    background-color: #0f1725;
    border: 1px solid #28364c;
    border-radius: 8px;
    font-weight: 600;
}
QPushButton#SubTab:hover {
    color: #eef3fb;
    background-color: #152035;
    border-color: #3b4f6e;
}
QPushButton#SubTab:checked {
    color: #ffffff;
    background-color: #1b2b4c;
    border-color: #4969aa;
}
QPushButton#PortToggle {
    min-height: 36px;
    padding: 0 10px;
    color: #7f8ca0;
    background-color: #0d1523;
    border: 1px solid #2a374c;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#PortToggle:hover {
    color: #cdd6e4;
    background-color: #121d2d;
    border-color: #40516d;
}
QPushButton#PortToggle:checked {
    color: #dce4ff;
    background-color: #182a4b;
    border-color: #526fae;
}
QPushButton#PortToggle:checked:hover {
    background-color: #1c3157;
    border-color: #6785c4;
}
"""



STYLE += r"""
QFrame#CompactRow {
    background-color: #0d1625;
    border: 1px solid #223047;
    border-radius: 9px;
}
QFrame#FilterBar {
    background-color: transparent;
    border: none;
}
QLabel#MiniAvatar {
    color: #e8edff;
    background-color: #1b2b49;
    border: 1px solid #36527e;
    border-radius: 14px;
    font-size: 12px;
    font-weight: 700;
}
QLabel#SecureChip {
    color: #bfe9d0;
    background-color: #10251d;
    border: 1px solid #2f654d;
    border-radius: 8px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#WaitingChip,
QLabel#HealthWaiting,
QLabel#HealthGood,
QLabel#HealthBad,
QLabel#HealthLocal {
    border-radius: 8px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#WaitingChip,
QLabel#HealthWaiting {
    color: #f0d6a0;
    background-color: #292113;
    border: 1px solid #6a542c;
}
QLabel#HealthGood {
    color: #bfe9d0;
    background-color: #10251d;
    border: 1px solid #2f654d;
}
QLabel#HealthBad {
    color: #ffc1bd;
    background-color: #2b181a;
    border: 1px solid #713940;
}
QLabel#HealthLocal {
    color: #c8d4ff;
    background-color: #17233e;
    border: 1px solid #344d7b;
}
QLabel#PeerDetail,
QLabel#ChatMeta {
    color: #71819a;
    font-size: 11px;
}
QLabel#ChatBody {
    color: #edf2fb;
    font-size: 13px;
}
QLabel#ChatEmpty {
    min-height: 120px;
    color: #73839a;
    font-size: 12px;
}
QLabel#ActionStatus {
    color: #b9c7db;
    background-color: #0d1727;
    border: 1px solid #26364e;
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 12px;
}
QScrollArea#ChatMessagesScroll {
    background-color: #0b1320;
    border: 1px solid #202e43;
    border-radius: 10px;
    padding: 6px;
}
QFrame#ChatBubble,
QFrame#ChatBubbleOwn {
    background-color: #0d1727;
    border: 1px solid #25354d;
    border-radius: 10px;
}
QFrame#ChatBubbleOwn {
    background-color: #142445;
    border-color: #345386;
}
QPushButton#Small,
QPushButton#SmallPrimary,
QPushButton#PortAction {
    min-height: 30px;
    padding: 0 11px;
    border-radius: 8px;
    font-size: 12px;
}
QPushButton#PortAction {
    color: #cbd7e8;
    background-color: #111d2e;
    border: 1px solid #344968;
}
QPushButton#PortAction:hover {
    color: #f3f7fd;
    background-color: #182840;
    border-color: #5574a1;
}
QPushButton#PortAction:pressed {
    background-color: #0d1727;
    border-color: #6f89b5;
}
QPushButton#PortAction[applied="true"] {
    color: #dff9ec;
    background-color: #15372f;
    border-color: #3f967b;
}
QPushButton#PortAction:disabled {
    color: #66758b;
    background-color: #0d1624;
    border-color: #26354b;
}
QPushButton#SmallPrimary {
    color: #ffffff;
    background-color: #6079f6;
    border-color: #7289fa;
}
QPushButton#SmallPrimary:hover {
    background-color: #6d85ff;
    border-color: #8296ff;
}
"""



STYLE += r"""
QLabel#Title { font-size: 26px; }
QLabel#Heading { font-size: 18px; }
QPushButton {
    min-height: 34px;
    padding: 0 14px;
    border-radius: 9px;
}
QPushButton#Primary {
    min-height: 38px;
    padding: 0 17px;
}
QLineEdit,
QSpinBox,
QComboBox {
    min-height: 34px;
    padding: 0 10px;
}
QPushButton#Nav,
QPushButton#SettingsButton {
    min-height: 32px;
}
QFrame#Card { border-radius: 12px; }
QFrame#ProcessCard { border-radius: 12px; }
"""
