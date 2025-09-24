from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Union
import asyncio
import aiohttp
from bs4 import BeautifulSoup
import datetime
import json
import logging
from urllib.parse import urlencode, quote
import re
import time

# 設定日誌記錄
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 建立 FastAPI 應用程式
app = FastAPI(
    title="政府採購網爬蟲 API",
    description="提供政府採購網資料爬取服務 - ZN Studio 製作",
    version="1.4.0",
    contact={
        "name": "Nick Chang",
        "email": "nickleo051216@gmail.com",
        "url": "https://portaly.cc/zn.studio"
    },
    license_info={
        "name": "MIT",
        "url": "https://opensource.org/licenses/MIT"
    }
)

class ScrapeRequest(BaseModel):
    search_terms: List[str] = Field(
        default=["系統", "平台", "建置", "維運"], 
        description="搜尋關鍵字列表",
        example=["系統", "平台", "建置"]
    )
    start_date: Optional[str] = Field(
        default=None, 
        description="開始日期 (YYYY/MM/DD 格式)",
        example="2024/09/01"
    )
    end_date: Optional[str] = Field(
        default=None, 
        description="結束日期 (YYYY/MM/DD 格式)",
        example="2024/09/24"
    )
    page_size: int = Field(
        default=100, 
        ge=1, 
        le=1000, 
        description="每頁筆數 (1-1000)",
        example=100
    )
    tender_type: Optional[str] = Field(
        default=None,
        description="招標方式過濾 (公開招標、限制性招標等)",
        example="公開招標"
    )
    min_budget: Optional[int] = Field(
        default=None,
        description="最低預算金額過濾",
        example=1000000
    )
    agency_filter: Optional[str] = Field(
        default=None,
        description="機關名稱過濾關鍵字",
        example="環保署"
    )

    @validator('start_date', 'end_date')
    def validate_date_format(cls, v):
        if v is None:
            return v
        try:
            datetime.datetime.strptime(v, '%Y/%m/%d')
            return v
        except ValueError:
            raise ValueError('日期格式必須為 YYYY/MM/DD')

class ScrapeResponse(BaseModel):
    success: bool = Field(description="是否成功")
    data: List[Dict] = Field(description="爬取資料")
    count: int = Field(description="資料筆數")
    message: str = Field(description="回應訊息")
    timestamp: str = Field(description="時間戳記")
    filters_applied: Dict = Field(description="套用的過濾條件")

class HealthResponse(BaseModel):
    status: str
    timestamp: str
    service: str
    version: str
    uptime_seconds: Optional[float] = None

class PCCWebScraper:
    """政府採購網爬蟲類別 - 標案名稱修正版"""
    
    def __init__(self):
        self.base_url = "https://web.pcc.gov.tw/prkms/tender/common/basic/readTenderBasic"
        self.session = None
        self.start_time = datetime.datetime.now()
        self.request_count = 0
        
    async def init_session(self):
        if not self.session:
            connector = aiohttp.TCPConnector(
                limit=10, 
                ssl=False,
                limit_per_host=3,
                ttl_dns_cache=300,
                use_dns_cache=True,
            )
            timeout = aiohttp.ClientTimeout(total=60, connect=30)
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
                    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache'
                }
            )
    
    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None
    
    def get_uptime(self) -> float:
        return (datetime.datetime.now() - self.start_time).total_seconds()
    
    async def make_request_with_retry(self, url: str, max_retries: int = 3) -> str:
        """帶重試機制的請求方法"""
        for attempt in range(max_retries):
            try:
                self.request_count += 1
                
                if self.request_count > 1:
                    await asyncio.sleep(min(2 + attempt, 5))
                
                async with self.session.get(url) as response:
                    if response.status == 403:
                        logger.warning(f"403 Forbidden - 可能需要登入或權限不足")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(10)
                            continue
                        return ""
                    
                    if response.status == 200:
                        html_content = await response.text(encoding='utf-8')
                        
                        if "系統錯誤訊息" in html_content or "使用者無權限操作此功能" in html_content:
                            logger.warning(f"收到錯誤頁面，狀態碼: {response.status}")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(5 + attempt * 2)
                                continue
                            return ""
                        
                        return html_content
                    else:
                        logger.warning(f"請求失敗，狀態碼: {response.status}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(3 + attempt)
                            continue
                        return ""
                        
            except Exception as e:
                logger.error(f"請求發生錯誤 (第 {attempt + 1} 次嘗試): {str(e)}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5 + attempt * 2)
                    continue
                return ""
        
        return ""
    
    async def scrape_by_keyword(self, keyword: str, start_date: str, end_date: str, page_size: int = 100) -> List[Dict]:
        if not self.session:
            await self.init_session()
            
        params = {
            'pageSize': page_size,
            'tenderStartDate': start_date,
            'tenderEndDate': end_date,
            'tenderName': keyword,
            'dateType': 'isDate'
        }
        query_string = urlencode(params, quote_via=quote)
        full_url = f"{self.base_url}?{query_string}"
        
        logger.info(f"正在爬取關鍵字: {keyword} | 日期範圍: {start_date} - {end_date}")
        
        html_content = await self.make_request_with_retry(full_url)
        if not html_content:
            logger.error(f"無法取得關鍵字 {keyword} 的資料")
            return []
        
        return self.parse_html_content(html_content, keyword)
    
    def clean_text(self, text: str) -> str:
        """清理文字，移除多餘空白和換行"""
        if not text:
            return ""
        text = re.sub(r'&[a-zA-Z]+;', ' ', text)
        text = re.sub(r'\s+', ' ', text.strip())
        return text
    
    def improved_parse_tender_info(self, cell_content: str) -> tuple:
        """改進版標案編號和名稱解析 - 修正標案名稱為空的問題"""
        if not cell_content:
            return "", ""
        
        # 移除HTML標籤，保留換行
        cell_content = re.sub(r'<[^>]+>', '\n', cell_content)
        
        # 分割行並清理，保留所有非空行
        lines = [line.strip() for line in cell_content.split('\n') if line.strip()]
        
        if len(lines) == 0:
            return "", ""
        elif len(lines) == 1:
            # 只有一行的情況
            single_line = lines[0]
            
            # 檢查是否為純編號格式
            if re.match(r'^[A-Za-z0-9\-_]{3,30}$', single_line) and len(single_line) <= 30:
                return single_line, ""
            # 檢查是否為更正公告
            elif "(更正公告)" in single_line or "(變更公告)" in single_line:
                # 嘗試從中提取編號
                clean_line = re.sub(r'\([^)]*\)', '', single_line).strip()
                if clean_line and re.match(r'^[A-Za-z0-9\-_]{3,30}$', clean_line):
                    return clean_line, "(更正公告)"
                else:
                    return "", single_line
            else:
                # 既不是純編號也不是更正公告，當作名稱
                return "", single_line
        else:
            # 多行情況 - 關鍵修正點
            first_line = lines[0]
            remaining_lines = lines[1:]
            
            # 處理第一行包含更正公告的情況
            if "(更正公告)" in first_line or "(變更公告)" in first_line:
                clean_first = re.sub(r'\([^)]*\)', '', first_line).strip()
                if clean_first and re.match(r'^[A-Za-z0-9\-_]{3,30}$', clean_first):
                    # 第一行去掉更正公告後是編號，其他行是名稱
                    if remaining_lines:
                        return clean_first, ' '.join(remaining_lines)
                    else:
                        return clean_first, "(更正公告)"
                else:
                    # 第一行去掉更正公告後不是編號，整體當名稱
                    return "", ' '.join(lines)
            
            # 檢查第一行是否像編號
            if (re.match(r'^[A-Za-z0-9\-_]{3,30}$', first_line) and 
                len(first_line) <= 30 and
                not re.search(r'[\u4e00-\u9fff]', first_line)):  # 不包含中文
                
                # 第一行是編號，其他行是名稱
                tender_name = ' '.join(remaining_lines) if remaining_lines else ""
                return first_line, tender_name
            else:
                # 第一行不像編號
                # 嘗試從所有行中找編號格式
                found_number = ""
                name_parts = []
                
                for line in lines:
                    # 清理後檢查是否為編號
                    clean_line = re.sub(r'\([^)]*\)', '', line).strip()
                    if (not found_number and 
                        re.match(r'^[A-Za-z0-9\-_]{3,30}$', clean_line) and 
                        len(clean_line) <= 30 and
                        not re.search(r'[\u4e00-\u9fff]', clean_line)):
                        found_number = clean_line
                    else:
                        # 不是編號的行加入名稱
                        if line.strip() != clean_line:  # 有被清理掉內容（如更正公告）
                            name_parts.append(line)
                        elif not found_number or line != clean_line:  # 不是編號或有額外內容
                            name_parts.append(line)
                
                tender_name = ' '.join(name_parts) if name_parts else ""
                return found_number, tender_name
    
    def parse_html_content(self, html: str, keyword: str) -> List[Dict]:
        """使用改進版解析的HTML內容解析"""
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        
        table = soup.find('table', {'id': 'tpam'}) or soup.find('table', {'class': 'tb_01'})
        if not table:
            logger.warning(f"關鍵字 {keyword} 未找到資料表格")
            return []
        
        rows = table.find_all('tr')
        today = datetime.date.today().strftime('%Y/%m/%d')
        
        data_rows = [row for row in rows if len(row.find_all('td')) >= 9]
        
        for row in data_rows:
            cols = row.find_all('td')
            if len(cols) < 9:
                continue
                
            try:
                項次 = self.clean_text(cols[0].get_text())
                機關名稱 = self.clean_text(cols[1].get_text())
                
                # 使用改進版解析函數
                標案資訊_cell = cols[2]
                標案資訊_text = 標案資訊_cell.get_text('\n').strip()
                標案編號, 標案名稱 = self.improved_parse_tender_info(標案資訊_text)
                
                傳輸次數 = self.clean_text(cols[3].get_text())
                招標方式 = self.clean_text(cols[4].get_text())
                採購性質 = self.clean_text(cols[5].get_text())
                公告日期 = self.clean_text(cols[6].get_text())
                截止投標 = self.clean_text(cols[7].get_text())
                預算金額 = self.clean_text(cols[8].get_text())

                href_url = ""
                a_tag = 標案資訊_cell.find("a")
                if a_tag and a_tag.get('href'):
                    href = a_tag['href']
                    if 'pk=' in href:
                        pk_val = href.split('pk=')[-1].split('&')[0]
                        href_url = f"https://web.pcc.gov.tw/tps/QueryTender/query/searchTenderDetail?pkPmsMain={pk_val}"
                
                預算金額_數字 = self.parse_budget_amount(預算金額)
                
                # 資料驗證
                if not 機關名稱 or (not 標案編號 and not 標案名稱):
                    logger.debug(f"跳過無效資料: 機關={機關名稱}, 編號={標案編號}, 名稱={標案名稱}")
                    continue
                
                final_data = {
                    "項次": 項次,
                    "機關名稱": 機關名稱,
                    "標案編號": 標案編號,
                    "標案名稱": 標案名稱,
                    "傳輸次數": 傳輸次數,
                    "招標方式": 招標方式,
                    "採購性質": 採購性質,
                    "公告日期": 公告日期,
                    "截止投標": 截止投標,
                    "預算金額": 預算金額,
                    "預算金額_數字": 預算金額_數字,
                    "網址": href_url,
                    "爬取日期": today,
                    "關鍵字": keyword,
                    # 除錯欄位
                    "debug_原始標案資訊": 標案資訊_text[:100] + "..." if len(標案資訊_text) > 100 else 標案資訊_text
                }

                results.append(final_data)

            except Exception as e:
                logger.error(f"解析行資料時發生錯誤: {str(e)}")
                logger.debug(f"問題行內容: {[col.get_text()[:50] for col in cols[:3]]}")
                continue

        logger.info(f"關鍵字 {keyword} 成功解析 {len(results)} 筆資料")
        return results
    
    def parse_budget_amount(self, budget_str: str) -> Optional[int]:
        """解析預算金額字串為數字"""
        if not budget_str or budget_str in ["未定", "依契約辦理", "不公開"]:
            return None
        try:
            clean_str = re.sub(r'[^\d]', '', budget_str)
            if clean_str:
                return int(clean_str)
        except:
            pass
        return None
    
    def apply_filters(self, results: List[Dict], filters: Dict) -> List[Dict]:
        """套用過濾條件"""
        filtered_results = results
        
        if filters.get('tender_type'):
            tender_type = filters['tender_type']
            filtered_results = [r for r in filtered_results if tender_type in r.get('招標方式', '')]
        
        if filters.get('min_budget'):
            min_budget = filters['min_budget']
            filtered_results = [r for r in filtered_results if 
                              r.get('預算金額_數字') and r['預算金額_數字'] >= min_budget]
        
        if filters.get('agency_filter'):
            agency_keyword = filters['agency_filter']
            filtered_results = [r for r in filtered_results if 
                              agency_keyword in r.get('機關名稱', '')]
        
        return filtered_results
    
    async def scrape_multiple_keywords(self, keywords: List[str], start_date: str, end_date: str, 
                                     page_size: int = 100, filters: Dict = None) -> List[Dict]:
        all_results = []
        for i, keyword in enumerate(keywords):
            if i > 0:
                await asyncio.sleep(3)
            
            keyword_results = await self.scrape_by_keyword(keyword, start_date, end_date, page_size)
            all_results.extend(keyword_results)
        
        if filters:
            all_results = self.apply_filters(all_results, filters)
        
        # 去重
        seen = set()
        unique_results = []
        for result in all_results:
            identifier = f"{result.get('機關名稱', '')}|{result.get('標案編號', '')}|{result.get('標案名稱', '')}"
            if identifier not in seen:
                seen.add(identifier)
                unique_results.append(result)
        
        return unique_results

scraper = PCCWebScraper()

@app.on_event("startup")
async def startup_event():
    await scraper.init_session()
    logger.info("政府採購網爬蟲 API 服務已啟動 - 標案名稱修正版 1.4.0")

@app.on_event("shutdown")
async def shutdown_event():
    await scraper.close_session()
    logger.info("政府採購網爬蟲 API 服務已關閉")

@app.get("/", response_class=HTMLResponse)
async def root():
    """API 首頁"""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>政府採購網爬蟲 API v1.4</title>
        <meta charset="utf-8">
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                     color: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
            .api-info { background: #f8f9fa; padding: 15px; border-radius: 8px; margin-bottom: 15px; }
            .contact { background: #e3f2fd; padding: 15px; border-radius: 8px; }
            .changelog { background: #fff3e0; padding: 15px; border-radius: 8px; margin-bottom: 15px; }
            a { color: #1976d2; text-decoration: none; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🚀 政府採購網爬蟲 API v1.4</h1>
            <p>版本: 1.4.0 | 服務狀態: 運行中 | 修正標案名稱為空問題</p>
        </div>
        
        <div class="changelog">
            <h2>🔧 v1.4.0 重要修正</h2>
            <ul>
                <li>✅ 修正標案名稱大量為空的問題</li>
                <li>✅ 改進多行內容的編號名稱分離邏輯</li>
                <li>✅ 優化更正公告的處理方式</li>
                <li>✅ 提升標案資訊解析完整性</li>
                <li>✅ 保持編號解析的高精確度</li>
            </ul>
        </div>
        
        <div class="api-info">
            <h2>📋 API 端點</h2>
            <ul>
                <li><strong>GET /docs</strong> - Swagger UI 互動式文件</li>
                <li><strong>GET /redoc</strong> - ReDoc 文件</li>
                <li><strong>GET /health</strong> - 健康狀態檢查</li>
                <li><strong>POST /scrape</strong> - 靈活爬蟲搜尋 (推薦)</li>
                <li><strong>POST /scrape-today</strong> - 今日快速搜尋</li>
                <li><strong>POST /test-parse</strong> - 測試解析功能</li>
            </ul>
        </div>
        
        <div class="contact">
            <h2>👨‍💻 開發者資訊</h2>
            <p><strong>Nick Chang</strong><br>
            📧 nickleo051216@gmail.com<br>
            📱 0932-684-051<br>
            🌐 <a href="https://portaly.cc/zn.studio">ZN Studio</a><br>
            📱 <a href="https://www.threads.com/@nickai216">Threads: @nickai216</a><br>
            💬 <a href="https://reurl.cc/1OZNAY">Line 社群</a><br>
            🤖 <a href="https://lin.ee/Faz0doj">Line OA</a></p>
        </div>
    </body>
    </html>
    """
    return html_content

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """健康狀態檢查"""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.datetime.now().isoformat(),
        service="政府採購網爬蟲",
        version="1.4.0",
        uptime_seconds=scraper.get_uptime()
    )

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_tenders(request: ScrapeRequest):
    """
    靈活的政府採購網爬蟲 - v1.4標案名稱修正版
    
    主要修正：
    - 解決標案名稱大量為空的問題
    - 改進多行內容的解析邏輯
    - 優化編號和名稱的分離算法
    """
    if not request.start_date:
        request.start_date = datetime.date.today().strftime('%Y/%m/%d')
    if not request.end_date:
        request.end_date = datetime.date.today().strftime('%Y/%m/%d')
    
    filters = {}
    if request.tender_type:
        filters['tender_type'] = request.tender_type
    if request.min_budget:
        filters['min_budget'] = request.min_budget
    if request.agency_filter:
        filters['agency_filter'] = request.agency_filter
    
    try:
        results = await scraper.scrape_multiple_keywords(
            keywords=request.search_terms,
            start_date=request.start_date,
            end_date=request.end_date,
            page_size=request.page_size,
            filters=filters if filters else None
        )
        
        n8n_format_results = [{"json": item} for item in results]
        
        return ScrapeResponse(
            success=True,
            data=n8n_format_results,
            count=len(results),
            message=f"成功爬取 {len(results)} 筆資料 (v1.4.0標案名稱修正版)",
            timestamp=datetime.datetime.now().isoformat(),
            filters_applied={
                "keywords": request.search_terms,
                "date_range": f"{request.start_date} ~ {request.end_date}",
                "page_size": request.page_size,
                **filters
            }
        )
        
    except Exception as e:
        logger.error(f"爬蟲執行錯誤: {str(e)}")
        raise HTTPException(status_code=500, detail=f"爬蟲執行失敗: {str(e)}")

@app.post("/scrape-today")
async def scrape_today(
    keywords: Optional[str] = Query(default="環境監測,土壤,地下水,環境", description="關鍵字，用逗號分隔"),
    page_size: Optional[int] = Query(default=100, description="每頁筆數")
):
    """今日快速爬蟲 - v1.4修正版"""
    today = datetime.date.today().strftime('%Y/%m/%d')
    keyword_list = [k.strip() for k in keywords.split(',')]
    
    try:
        results = await scraper.scrape_multiple_keywords(
            keywords=keyword_list,
            start_date=today,
            end_date=today,
            page_size=page_size
        )
        
        n8n_format_results = [{"json": item} for item in results]
        return n8n_format_results
        
    except Exception as e:
        logger.error(f"今日爬蟲執行錯誤: {str(e)}")
        raise HTTPException(status_code=500, detail=f"今日爬蟲執行失敗: {str(e)}")

@app.get("/api-status")
async def api_status():
    """詳細的API狀態資訊"""
    return {
        "service": "政府採購網爬蟲 API",
        "version": "1.4.0",
        "status": "running",
        "uptime_seconds": scraper.get_uptime(),
        "request_count": scraper.request_count,
        "changelog": {
            "v1.4.0": [
                "修正標案名稱大量為空的問題",
                "改進多行內容的編號名稱分離邏輯", 
                "優化更正公告的處理方式",
                "提升標案資訊解析完整性",
                "保持編號解析的高精確度"
            ]
        },
        "author": "Nick Chang｜nickleo051216@gmail.com｜0932-684-051",
        "contact": {
            "website": "https://portaly.cc/zn.studio",
            "threads": "https://www.threads.com/@nickai216", 
            "line_community": "https://reurl.cc/1OZNAY",
            "line_oa": "https://lin.ee/Faz0doj"
        },
        "timestamp": datetime.datetime.now().isoformat()
    }

@app.post("/test-parse")
async def test_parse_tender_info(content: str):
    """測試標案編號名稱解析功能"""
    編號, 名稱 = scraper.improved_parse_tender_info(content)
    return {
        "原始內容": content,
        "解析結果": {
            "標案編號": 編號,
            "標案名稱": 名稱
        },
        "說明": "v1.4.0 改進版解析測試"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
