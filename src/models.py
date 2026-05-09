from pydantic import BaseModel


class Hypothesis(BaseModel):
    id: int
    title: str
    description: str
    category: str
    file_path: str | None = None
    line_number: int | None = None
    side: str = "RIGHT"
    severity: str = "medium"


class HypothesesOutput(BaseModel):
    hypotheses: list[Hypothesis]


class ReviewComment(BaseModel):
    path: str
    line: int
    start_line: int | None = None
    side: str = "RIGHT"
    title: str = ""
    details: str = ""
    suggestion: str | None = None
    body: str = ""
    category: str = "general"
    severity: str = "medium"


class ReviewCommentList(BaseModel):
    comments: list[ReviewComment]


class ReviewOutput(BaseModel):
    summary: str
    comments: list[ReviewComment]


class ConversationReply(BaseModel):
    body: str


class ReviewRecheckOutput(BaseModel):
    resolved: bool
    body: str
