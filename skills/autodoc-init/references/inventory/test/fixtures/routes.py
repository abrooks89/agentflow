from fastapi import APIRouter
router = APIRouter()

@router.post("/api/projects")
def create_project(body):
    return body

def plain_helper(x):
    return x
