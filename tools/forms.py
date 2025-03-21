from django import forms
from .models import Tool, Tag


class ToolEditForm(forms.ModelForm):
    class Meta:
        model = Tool
        fields = ["name", "github_url", "description", "tags"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "tags": forms.CheckboxSelectMultiple(),
        }
