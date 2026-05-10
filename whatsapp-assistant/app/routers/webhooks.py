from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/webhooks")


@router.get("/whatsapp")
async def verify_whatsapp(request: Request) -> JSONResponse:
    """Meta webhook verification challenge."""
    params = request.query_params
    return JSONResponse({"status": "placeholder", "params": dict(params)})


@router.post("/whatsapp")
async def receive_whatsapp(request: Request) -> JSONResponse:
    """Receive incoming WhatsApp messages from Meta."""
    return JSONResponse({"status": "received"})
