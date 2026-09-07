from pathlib import Path
import json
import re
from playwright.sync_api import sync_playwright, expect


root = Path(__file__).resolve().parents[1]
artifacts = root / 'test-artifacts'
artifacts.mkdir(exist_ok=True)
errors = []
with sync_playwright() as playwright:
    try:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
    except Exception:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception:
            browser = playwright.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    context = browser.new_context(viewport={'width':1440,'height':1100}, reduced_motion='reduce')
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://127.0.0.1:4173')
    expect(page.locator('#cards .card')).to_have_count(6)
    expect(page.locator('#cards .card').first.locator('.photo img')).to_be_visible()
    expect(page.locator('#cards .card').first.locator('.actions .open')).to_have_attribute('href', re.compile(r'^https://'))
    expect(page.locator('#cards .card').first.locator('.facts > div')).to_have_count(6)
    page.screenshot(path=str(artifacts/'desktop.png'),full_page=True)
    page.locator('.save-btn').first.click()
    page.locator('[data-collection="saved"]').click()
    expect(page.locator('#cards .card')).to_have_count(1)
    page.locator('.hide-btn').click()
    expect(page.locator('#cards .card')).to_have_count(0)
    page.locator('[data-collection="hidden"]').click()
    expect(page.locator('#cards .card')).to_have_count(1)
    page.locator('.hide-btn').click()
    page.locator('[data-collection="all"]').click()
    expect(page.locator('#cards .card')).to_have_count(6)
    page.locator('.compare-check input').nth(0).check()
    page.locator('.compare-check input').nth(1).check()
    page.locator('#openCompare').click()
    expect(page.locator('#compareDialog')).to_be_visible()
    expect(page.locator('#compareBody thead th')).to_have_count(3)
    page.locator('#closeCompare').click()
    page.locator('#clearCompare').click()
    page.locator('[data-detail]').first.click()
    page.locator('#listingNote').fill('Sprawdzić dojazd i warunki przyłącza.')
    page.locator('#detailClose').click()
    page.locator('[data-detail]').first.click()
    expect(page.locator('#listingNote')).to_have_value('Sprawdzić dojazd i warunki przyłącza.')
    page.locator('#detailClose').click()
    page.locator('#workspaceSettings').click()
    page.locator('[data-section="metrics"]').uncheck()
    page.locator('#darkTheme').check()
    page.locator('[data-close-workspace]').click()
    page.reload()
    expect(page.locator('#cards .card')).to_have_count(6)
    expect(page.locator('.summary-grid')).not_to_be_visible()
    expect(page.locator('html')).to_have_attribute('data-theme','dark')
    page.screenshot(path=str(artifacts/'dark.png'),full_page=True)
    page.locator('#workspaceSettings').click()
    page.locator('#resetWorkspace').click()
    page.locator('[data-close-workspace]').click()
    page.locator('#filtersBtn').click()
    expect(page.locator('#filters')).to_have_attribute('aria-hidden','false')
    page.locator('#maxPrice').fill('200000')
    page.locator('#applyFilters').click()
    expect(page.locator('#cards .card')).to_have_count(4)
    page.locator('#filtersBtn').click()
    page.locator('#clearFilters').click()
    page.keyboard.press('Escape')
    expect(page.locator('#filters')).to_have_attribute('aria-hidden','true')
    expect(page.locator('#filtersBtn')).to_be_focused()
    page.locator('#sort').select_option('priceAsc')
    expect(page.locator('#cards .card').last).to_contain_text('Blisko centrum')
    page.locator('#sort').select_option('freshness')
    for width in (390,320,768):
        page.set_viewport_size({'width':width,'height':844})
        page.evaluate('window.scrollTo(0,0)')
        expect(page.locator('#cards .card')).to_have_count(6)
        overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not overflow, f'Horizontal overflow at {width}px'
        page.screenshot(path=str(artifacts/f'mobile-{width}.png'),full_page=True)
        page.locator('[data-layout="list"]').click()
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), f'List overflow at {width}px'
        page.locator('[data-layout="grid"]').click()
    context.close()
    browser.close()
assert not errors, errors
print(json.dumps({'ui':'PASS','viewports':[1440,768,390,320],'console_errors':errors,'checks':['save','hide/restore','compare','notes','theme persistence','section persistence','price filter with missing price','keyboard filters','sort unknown last','responsive grid/list']},ensure_ascii=False))
