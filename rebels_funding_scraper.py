#!/usr/bin/env python3
"""
Rebels Funding - Complete Trade Downloader
Downloads Excel files for ALL traders across ALL competitions (Aug 2023 - May 2026).
Skips latest month (June 2026). Resumes where it left off.
"""

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
import time, os, re, glob

# All competition IDs from scan (Aug 2023 to May 2026)
# June 2026 (ID 1336) is skipped as latest
COMPETITIONS = [
    (1330, "Competition-05-26"),
    (1322, "Competition-04-26"),
    (1316, "Competition-03-26"),
    (1256, "Competition-02-26"),
    (1250, "Competition-01-26"),
    (1243, "Competition-12-25"),
    (1232, "Competition-11-25"),
    (1218, "Competition-10-25"),
    (1127, "Competition-09-25"),
    (1115, "Competition-08-25"),
    (1106, "Competition-07-25"),
    (1100, "Competition-06-25"),
    (1094, "Competition-05-25"),
    (1088, "Competition-04-25"),
    (1082, "Competition-03-25"),
    (1078, "Competition-02-25"),
    (1074, "Competition-01-25"),
    (1070, "Competition-12-24"),
    (1066, "Competition-11-24"),
    (1056, "Competition-10-24"),
    (1048, "Competition-09-24"),
    (1038, "Competition-08-24"),
    (1034, "Competition-07-24"),
    (1009, "Competition-06-24"),
    (997,  "Competition-05-24"),
    (993,  "Competition-04-24"),
    (986,  "Competition-03-24"),
    (973,  "Competition-02-24"),
    (967,  "Competition-01-24"),
    (956,  "Competition-12-23"),
    (951,  "Competition-10-23"),
    (943,  "Competition-09-23"),
    (890,  "Competition-08-23"),
]


def download_competition(driver, dl_dir, comp_id, comp_name):
    print(f"\n{'='*50}")
    print(f"📁 {comp_name} (ID: {comp_id})")
    print(f"{'='*50}")
    
    driver.get(f"https://rf-zone.rebelsfunding.com/leaderboard/history/{comp_id}")
    time.sleep(6)
    
    body = driver.find_element(By.TAG_NAME, "body").text
    if "404" in body[:50]:
        print("  ⚠ Not found")
        return 0
    
    rows = driver.find_elements(By.CSS_SELECTOR, "tr[role='row']")
    total = len(rows) - 1
    if total <= 0:
        print("  ⚠ No traders")
        return 0
    
    print(f"Traders: {total}")
    
    existing = set()
    for f in os.listdir(dl_dir):
        m = re.match(rf'{comp_name}_R(\d+)_', f)
        if m: existing.add(int(m.group(1)))
    
    if existing:
        print(f"Already have: {len(existing)} (resuming from rank {max(existing)+1})")
    
    ok = fail = 0
    for idx in range(1, len(rows)):
        if idx in existing:
            continue
        
        try:
            name_el = rows[idx].find_element(By.CSS_SELECTOR, "td:nth-child(2) span")
            name = name_el.text.strip()
        except:
            rows = driver.find_elements(By.CSS_SELECTOR, "tr[role='row']")
            if idx >= len(rows): break
            try:
                name_el = rows[idx].find_element(By.CSS_SELECTOR, "td:nth-child(2) span")
                name = name_el.text.strip()
            except:
                continue
        
        if not name or name in ("#", "Name"): continue
        
        print(f"  [{idx}/{total}] {name[:30]:30s}", end=" ", flush=True)
        
        try:
            ActionChains(driver).move_to_element(name_el).click(name_el).perform()
            time.sleep(3)
            
            export_btns = driver.find_elements(By.XPATH, "//span[contains(text(), 'Excel export')]")
            if export_btns and export_btns[0].is_displayed():
                export_btns[0].find_element(By.XPATH, "./ancestor::button").click()
                time.sleep(5)
            
            try: driver.find_element(By.CSS_SELECTOR, ".rf-modal-close").click(); time.sleep(1)
            except: pass
            
            files = sorted(glob.glob(os.path.join(dl_dir, "trades_*.xlsx*")), key=os.path.getmtime)
            if files:
                safe = re.sub(r'[^\w\s-]', '', name).strip()[:30]
                new = os.path.join(dl_dir, f"{comp_name}_R{idx}_{safe}.xlsx")
                c = 1
                while os.path.exists(new):
                    new = os.path.join(dl_dir, f"{comp_name}_R{idx}_{safe}_{c}.xlsx"); c += 1
                os.rename(files[-1], new)
            
            ok += 1
            print("✅")
        except:
            fail += 1
            print("❌")
            try: driver.find_element(By.CSS_SELECTOR, ".rf-modal-close").click()
            except: pass
        
        time.sleep(1)
        if idx % 15 == 0:
            rows = driver.find_elements(By.CSS_SELECTOR, "tr[role='row']")
    
    print(f"\n  ✅ {ok} new  ❌ {fail} errors")
    return ok


def main():
    options = Options()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    dl_dir = os.path.join(os.getcwd(), "trader_trades")
    os.makedirs(dl_dir, exist_ok=True)
    prefs = {"download.default_directory": dl_dir, "download.prompt_for_download": False, "safebrowsing.enabled": False}
    options.add_experimental_option("prefs", prefs)
    
    driver = webdriver.Chrome(options=options)
    grand_total = 0
    
    try:
        print(f"Competitions to process: {len(COMPETITIONS)} (Aug 2023 - May 2026)")
        print(f"Each has ~150 traders\n")
        
        for comp_id, comp_name in COMPETITIONS:
            grand_total += download_competition(driver, dl_dir, comp_id, comp_name)
        
        print(f"\n{'='*50}")
        print(f"🎉 ALL DONE! {grand_total} new files downloaded")
        print(f"📂 {dl_dir}")
        print(f"{'='*50}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
