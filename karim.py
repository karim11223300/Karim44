from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes

# الحالات
ASK_CODE, ASK_GB, ASK_COUNTRY = range(3)

# أعلام الدول العربية
countries_flags = {
    "مصر": "🇪🇬", "السعودية": "🇸🇦", "الإمارات": "🇦🇪", "الكويت": "🇰🇼",
    "قطر": "🇶🇦", "البحرين": "🇧🇭", "عمان": "🇴🇲", "الأردن": "🇯🇴",
    "سوريا": "🇸🇾", "لبنان": "🇱🇧", "فلسطين": "🇵🇸", "العراق": "🇮🇶",
    "اليمن": "🇾🇪", "ليبيا": "🇱🇾", "تونس": "🇹🇳", "الجزائر": "🇩🇿",
    "المغرب": "🇲🇦", "موريتانيا": "🇲🇷", "الصومال": "🇸🇴", "جيبوتي": "🇩🇯",
    "جزر القمر": "🇰🇲", "السودان": "🇸🇩"
}

# الهروب ل MarkdownV2
def esc(text: str) -> str:
    return (text.replace("-", "\\-")
                .replace(".", "\\.")
                .replace("(", "\\(")
                .replace(")", "\\)")
                .replace("=", "\\="))

# بدء المحادثة
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 أهلاً! ابعتلي الكود الأول.")
    return ASK_CODE

# استلام الكود
async def ask_gb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["code"] = update.message.text.strip()
    await update.message.reply_text("تمام ✅، دلوقتي ابعت عدد الجيجا:")
    return ASK_GB

# استلام عدد الجيجا
async def ask_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["gb"] = update.message.text.strip()
    await update.message.reply_text("حلو 👌، دلوقتي اكتب اسم الدولة بالعربي:")
    return ASK_COUNTRY

# استلام الدولة وتجهيز الكليشة
async def show_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    country = update.message.text.strip()
    flag = countries_flags.get(country, "🏳️")

    code = context.user_data["code"]
    gb = esc(context.user_data["gb"])

    # تقسيم الكود للآيفون مع سطر فاصل
    if code.startswith("LPA:1$"):
        parts = code.split("$")
        if len(parts) >= 3:
            iphone_code = f"`{esc(parts[1])}`\n\n`{esc(parts[2])}`"
        else:
            iphone_code = f"`{esc(code)}`"
    else:
        iphone_code = f"`{esc(code)}`"

    result = f"""
*{gb} جيجا {country} فقط {flag} *

📱 *الكود أندرويد:*  
`{esc(code)}`

🍏 *الكود آيفون:*  
{iphone_code}

*الاسكرين بعد التفعيل\\. مع تحياتي Ꮶ Ξ Ꭱ Ꮎ*
"""

    await update.message.reply_text(result, parse_mode="MarkdownV2")
    return ConversationHandler.END

# إلغاء
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم الإلغاء ❌")
    return ConversationHandler.END

def main():
    app = Application.builder().token("7713061186:AAG2sbVP2WEicMoHIzFMxSYvgtFc5fhZSxg").build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_gb)],
            ASK_GB: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_country)],
            ASK_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, show_result)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.run_polling()

if __name__ == "__main__":
    main()
