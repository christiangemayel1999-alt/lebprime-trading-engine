cd "/c/Users/gemay/OneDrive/Desktop/Trading_Bot_Original/Trading Bot"
while ! grep -q "ALL DONE\|Traceback" reports/phase4/momentum_test.log 2>/dev/null; do sleep 30; done
python reports/phase4/confirm_entry_test.py > reports/phase4/confirm_entry_test.log 2>&1
