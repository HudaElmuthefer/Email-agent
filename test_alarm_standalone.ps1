# اختبار مستقل للارم (نفس سلوك app/duplicate_alert.py بالضبط) - لا يحتاج Python ولا أي تثبيت،
# فقط PowerShell المدمج مع ويندوز. يمكن نسخ هذا الملف لأي جهاز وتشغيله مباشرة.

Add-Type -AssemblyName System.Windows.Forms

for ($i = 0; $i -lt 3; $i++) {
    [console]::beep(1200, 250)
    [console]::beep(800, 250)
}

$text = "هذا اختبار فقط للارم (لا يوجد تكرار حقيقي).`n`n" +
        "الإيميل: اختبار`n" +
        "من: test@example.com`n" +
        "التاريخ: 2026-11-19 12:00`n`n" +
        "- الكتاب «test.pdf» بالعدد 123 والتاريخ 2026-11-19 تمت أرشفته سابقاً.`n`n" +
        "إذا شفتي هذه النافذة وسمعتي الصوت قبلها، فالارم يعمل تماماً على هذا الجهاز."

[System.Windows.Forms.MessageBox]::Show(
    $text,
    "تنبيه: مرفق مكرر - E-Mail AI AGENT",
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Warning,
    [System.Windows.Forms.MessageBoxDefaultButton]::Button1,
    [System.Windows.Forms.MessageBoxOptions]::RtlReading -bor [System.Windows.Forms.MessageBoxOptions]::RightAlign
) | Out-Null
