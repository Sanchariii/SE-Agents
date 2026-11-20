import { HttpClientModule, HTTP_INTERCEPTORS } from '@angular/common/http';
import { NgModule } from '@angular/core';
import { BrowserModule } from '@angular/platform-browser';
import { Activity, AlertTriangle, ArrowDown, ArrowUp, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, Download, RefreshCw, X, LucideAngularModule } from 'lucide-angular';

import { AppRoutingModule } from './app-routing.module';
import { AppComponent } from './app.component';
import { MainComponent } from './main/main.component';
import { LoginComponent } from './login/login.component';
import { TrainingStudioComponent } from './training-studio/training-studio.component';
import { QuestionFormComponent } from './training-studio/question-form/question-form.component';
import { PipelineComponent } from './pipeline/pipeline.component';
import { ApprovalsComponent } from './approvals/approvals.component';
import { AnalyticsComponent } from './analytics/analytics.component';
import { AnalyticsChartComponent } from './analytics/analytics-chart.component';
import { BootComponent } from './shared/components/boot/boot.component';
import { AppHeaderComponent } from './shared/components/app-header/app-header.component';
import { AuthInterceptor } from './shared/interceptors/auth.interceptor';
import { SharedModule } from './shared/shared.module';

@NgModule({
  declarations: [
    AppComponent, MainComponent, LoginComponent, TrainingStudioComponent, QuestionFormComponent,
    PipelineComponent, ApprovalsComponent, AnalyticsComponent, BootComponent, AppHeaderComponent,
  ],
  imports: [BrowserModule, HttpClientModule, SharedModule, AppRoutingModule, AnalyticsChartComponent,
    LucideAngularModule.pick({ Activity, AlertTriangle, ArrowDown, ArrowUp, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, Download, RefreshCw, X })],
  providers: [
    { provide: HTTP_INTERCEPTORS, useClass: AuthInterceptor, multi: true },
  ],
  bootstrap: [AppComponent],
})
export class AppModule {}
