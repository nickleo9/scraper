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

# 設定日誌記錄
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 建立 FastAPI 應用程式
app = FastAPI(
    title="政府採購網爬蟲 API",
    description="提供政府採購網資料爬取服務 - ZN Studio 製作",
    version="1.1.0",
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
    """政府採購網爬蟲類別 - 不需要 Selenium 的版本"""
    
    def __init__(self):
        self.base_url = "https://web.pcc.gov.tw/prkms/tender/common/basic/readTenderBasic"
        self.session = None
        self.start_time = datetime.datetime.now()
        
    async def init_session(self):
        if not self.session:
            connector = aiohttp.TCPConnector(limit=10, ssl=False)
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive'
                }
            )
    
    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None
    
    def get_uptime(self) -> float:
        return (datetime.datetime.now() - self.start_time).total_seconds()
    
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
        
        try:
            async with self.session.get(full_url) as response:
                if response.status != 200:
                    logger.error(f"請求失敗: {response.status}")
                    return []
                html_content = await response.text()
                return self.parse_html_content(html_content, keyword)
        except Exception as e:
            logger.error(f"爬取關鍵字 {keyword} 時發生錯誤: {str(e)}")
            return []
    
    def parse_html_content(self, html: str, keyword: str) -> List[Dict]:
        """解析網頁內容，分開案號跟名稱"""
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        table = soup.find('table', {'id': 'tpam'}) or soup.find('table', {'class': 'tb_01'})
        if not table:
            logger.warning(f"關鍵字 {keyword} 未找到資料表格")
            return []
        
        rows = table.find_all('tr')
        today = datetime.date.today().strftime('%Y/%m/%d')
        
        for row in rows[1:]:
            cols = row.find_all('td')
            if len(cols) < 9:
                continue
            try:
                # 建立基本資料字典
                row_data = [col.get_text("\n", strip=True) for col in cols[:9]]
                keys = ["項次", "機關名稱", "標案案號&編號名稱", "傳輸次數", "招標方式", "採購性質", "公告日期", "截止投標", "預算金額"]
                row_dict = dict(zip(keys, row_data))

                # 取得連結
                href_url = ""
                a_tag = cols[2].find("a")
                if a_tag and a_tag.get('href') and 'pk=' in a_tag.get('href'):
                    pk_val = a_tag['href'].split('pk=')[-1].split('&')[0]
                    href_url = f"https://web.pcc.gov.tw/tps/QueryTender/query/searchTenderDetail?pkPmsMain={pk_val}"
                
                # 分開案號與名稱
                raw = row_dict.pop("標案案號&編號名稱", "")
                if "\n" in raw:
                   line1, line2 = raw.split("\n", 1)
                   編號 = line1.split()[0] if line1.split() else ""
                   名稱 = line2.strip()
                else:
                   編號 = ""
                   名稱 = raw
                
                # 處理預算金額，轉換為數字便於過濾
                預算金額_原始 = row_dict.get("預算金額", "")
                預算金額_數字 = self.parse_budget_amount(預算金額_原始)
                    
                final_data = {
                    "項次": row_dict.get("項次", ""),
                    "機關名稱": row_dict.get("機關名稱", ""),
                    "標案編號": 編號,
                    "標案名稱": 名稱,
                    "傳輸次數": row_dict.get("傳輸次數", ""),
                    "招標方式": row_dict.get("招標方式", ""),
                    "採購性質": row_dict.get("採購性質", ""),
                    "公告日期": row_dict.get("公告日期", ""),
                    "截止投標": row_dict.get("截止投標", ""),
                    "預算金額": 預算金額_原始,
                    "預算金額_數字": 預算金額_數字,
                    "網址": href_url,
                    "爬取日期": today,
                    "關鍵字": keyword
                }

                if 名稱 and row_dict.get("機關名稱"):
                    results.append(final_data)

            except Exception as e:
                logger.error(f"解析行資料時發生錯誤: {str(e)}")
                continue

        logger.info(f"關鍵字 {keyword} 獲得 {len(results)} 筆資料")
        return results
    
    def parse_budget_amount(self, budget_str: str) -> Optional[int]:
        """將預算金額字串轉換為數字"""
        if not budget_str or budget_str == "未定":
            return None
        try:
            # 移除逗號和空格，提取數字
            clean_str = budget_str.replace(",", "").replace(" ", "")
            # 尋找數字部分
            import re
            numbers = re.findall(r'\d+', clean_str)
            if numbers:
                return int(numbers[0])
        except:
            pass
        return None
    
    def apply_filters(self, results: List[Dict], filters: Dict) -> List[Dict]:
        """套用過濾條件"""
        filtered_results = results
        
        # 招標方式過濾
        if filters.get('tender_type'):
            tender_type = filters['tender_type']
            filtered_results = [r for r in filtered_results if tender_type in r.get('招標方式', '')]
        
        # 最低預算過濾
        if filters.get('min_budget'):
            min_budget = filters['min_budget']
            filtered_results = [r for r in filtered_results if 
                              r.get('預算金額_數字') and r['預算金額_數字'] >= min_budget]
        
        # 機關名稱過濾
        if filters.get('agency_filter'):
            agency_keyword = filters['agency_filter']
            filtered_results = [r for r in filtered_results if 
                              agency_keyword in r.get('機關名稱', '')]
        
        return filtered_results
    
    async def scrape_multiple_keywords(self, keywords: List[str], start_date: str, end_date: str, 
                                     page_size: int = 100, filters: Dict = None) -> List[Dict]:
        all_results = []
        for i, keyword in enumerate(keywords):
            if i > 0:  # 第一個關鍵字不需要等待
                await asyncio.sleep(2)  # 避免對伺服器造成過大負擔
            keyword_results = await self.scrape_by_keyword(keyword, start_date, end_date, page_size)
            all_results.extend(keyword_results)
        
        # 套用過濾條件
        if filters:
            all_results = self.apply_filters(all_results, filters)
        
        # 去除重複的標案（根據標案編號）
        seen_numbers = set()
        unique_results = []
        for result in all_results:
            tender_number = result.get('標案編號', '')
            if tender_number and tender_number not in seen_numbers:
                seen_numbers.add(tender_number)
                unique_results.append(result)
            elif not tender_number:  # 沒有編號的也保留
                unique_results.append(result)
        
        return unique_results

scraper = PCCWebScraper()

@app.on_event("startup")
async def startup_event():
    await scraper.init_session()
    logger.info("政府採購網爬蟲 API 服務已啟動")

@app.on_event("shutdown")
async def shutdown_event():
    await scraper.close_session()
    logger.info("政府採購網爬蟲 API 服務已關閉")

@app.get("/", response_class=HTMLResponse)
async def root():
    """API 首頁 - 提供互動式文件"""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>政府採購網爬蟲 API</title>
        <meta charset="utf-8">
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                     color: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
            .api-info { background: #f8f9fa; padding: 15px; border-radius: 8px; margin-bottom: 15px; }
            .contact { background: #e3f2fd; padding: 15px; border-radius: 8px; }
            a { color: #1976d2; text-decoration: none; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🚀 政府採購網爬蟲 API</h1>
            <p>版本: 1.1.0 | 服務狀態: 運行中</p>
        </div>
        
        <div class="api-info">
            <h2>📋 API 端點</h2>
            <ul>
                <li><strong>GET /docs</strong> - Swagger UI 互動式文件</li>
                <li><strong>GET /redoc</strong> - ReDoc 文件</li>
                <li><strong>GET /health</strong> - 健康狀態檢查</li>
                <li><strong>POST /scrape</strong> - 靈活爬蟲搜尋 (推薦)</li>
                <li><strong>POST /scrape-today</strong> - 今日快速搜尋</li>
                <li><strong>GET /api-status</strong> - 詳細服務狀態</li>
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
        version="1.1.0",
        uptime_seconds=scraper.get_uptime()
    )

@app.get("/api-status")
async def api_status():
    """詳細的 API 狀態資訊"""
    return {
        "service": "政府採購網爬蟲 API",
        "version": "1.1.0",
        "status": "running",
        "uptime_seconds": scraper.get_uptime(),
        "endpoints": {
            "scrape": "POST /scrape - 靈活搜尋 (支援多種過濾條件)",
            "scrape_today": "POST /scrape-today - 今日快速搜尋",
            "health": "GET /health - 健康檢查",
            "docs": "GET /docs - Swagger 文件"
        },
        "features": [
            "多關鍵字搜尋",
            "日期範圍過濾",
            "預算金額過濾",
            "招標方式過濾",
            "機關名稱過濾",
            "重複資料自動去除",
            "n8n 相容格式輸出"
        ],
        "author": "Nick Chang｜nickleo051216@gmail.com｜0932-684-051",
        "contact": {
            "website": "https://portaly.cc/zn.studio",
            "threads": "https://www.threads.com/@nickai216",
            "line_community": "https://reurl.cc/1OZNAY",
            "line_oa": "https://lin.ee/Faz0doj"
        },
        "timestamp": datetime.datetime.now().isoformat()
    }

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_tenders(request: ScrapeRequest):
    """
    靈活的政府採購網爬蟲
    
    支援多種搜尋條件和過濾選項：
    - 自訂關鍵字列表
    - 指定日期範圍
    - 預算金額過濾
    - 招標方式過濾
    - 機關名稱過濾
    """
    # 設定預設日期
    if not request.start_date:
        request.start_date = datetime.date.today().strftime('%Y/%m/%d')
    if not request.end_date:
        request.end_date = datetime.date.today().strftime('%Y/%m/%d')
    
    # 準備過濾條件
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
        
        # n8n 相容格式 - 每筆資料包裝在 json 物件中
        n8n_format_results = [{"json": item} for item in results]
        
        return ScrapeResponse(
            success=True,
            data=n8n_format_results,
            count=len(results),
            message=f"成功爬取 {len(results)} 筆資料",
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
    """
    今日快速爬蟲 - 相容舊版 API
    
    保持原有的快速搜尋功能，但加入了參數化選項
    """
    today = datetime.date.today().strftime('%Y/%m/%d')
    keyword_list = [k.strip() for k in keywords.split(',')]
    
    try:
        results = await scraper.scrape_multiple_keywords(
            keywords=keyword_list,
            start_date=today,
            end_date=today,
            page_size=page_size
        )
        
        # 保持原有的回傳格式
        n8n_format_results = [{"json": item} for item in results]
        return n8n_format_results
        
    except Exception as e:
        logger.error(f"今日爬蟲執行錯誤: {str(e)}")
        raise HTTPException(status_code=500, detail=f"今日爬蟲執行失敗: {str(e)}")

# 新增批次查詢端點
@app.post("/scrape-batch")
async def scrape_batch(requests: List[ScrapeRequest]):
    """
    批次爬蟲查詢
    
    可以一次執行多個不同條件的搜尋
    """
    if len(requests) > 5:  # 限制批次數量
        raise HTTPException(status_code=400, detail="批次查詢最多支援 5 個請求")
    
    all_results = []
    for i, req in enumerate(requests):
        logger.info(f"執行批次查詢 {i+1}/{len(requests)}")
        
        if not req.start_date:
            req.start_date = datetime.date.today().strftime('%Y/%m/%d')
        if not req.end_date:
            req.end_date = datetime.date.today().strftime('%Y/%m/%d')
            
        filters = {}
        if req.tender_type:
            filters['tender_type'] = req.tender_type
        if req.min_budget:
            filters['min_budget'] = req.min_budget
        if req.agency_filter:
            filters['agency_filter'] = req.agency_filter
        
        try:
            batch_results = await scraper.scrape_multiple_keywords(
                keywords=req.search_terms,
                start_date=req.start_date,
                end_date=req.end_date,
                page_size=req.page_size,
                filters=filters if filters else None
            )
            
            # 標記批次來源
            for result in batch_results:
                result['batch_index'] = i + 1
                result['batch_keywords'] = req.search_terms
                
            all_results.extend(batch_results)
            
            # 批次間的等待時間
            if i < len(requests) - 1:
                await asyncio.sleep(3)
                
        except Exception as e:
            logger.error(f"批次查詢 {i+1} 執行錯誤: {str(e)}")
            continue
    
    n8n_format_results = [{"json": item} for item in all_results]
    
    return {
        "success": True,
        "data": n8n_format_results,
        "total_count": len(all_results),
        "batch_count": len(requests),
        "message": f"批次查詢完成，共獲得 {len(all_results)} 筆資料",
        "timestamp": datetime.datetime.now().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
