from fastapi import APIRouter

from . import (
    activity,
    auth,
    book,
    calendar,
    following,
    index,
    issues,
    init,
    library,
    login,
    recommendations,
    request,
    search,
    settings,
    static,
    system,
    wishlist,
)

router = APIRouter()

router.include_router(activity.router)
router.include_router(auth.router)
router.include_router(book.router)
router.include_router(calendar.router)
router.include_router(following.router)
router.include_router(index.router)
router.include_router(issues.router)
router.include_router(init.router)
router.include_router(library.router)
router.include_router(login.router)
router.include_router(recommendations.router)
router.include_router(request.router)
router.include_router(search.router)
router.include_router(settings.router)
router.include_router(static.router)
router.include_router(system.router)
router.include_router(wishlist.router)
